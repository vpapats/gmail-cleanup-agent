from __future__ import annotations

import os
import re
from typing import Any

import requests

from src.models import ClassificationResult, MessageContext


PROTECTION_PATTERNS = {
    "has_attachments": lambda m: m.has_attachments,
    "reply_thread": lambda m: m.is_reply_thread,
    "financial": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        ["invoice", "receipt", "payment", "quote", "contract", "tax"],
    ),
    "legal_or_compliance": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        ["legal", "compliance", "regulatory", "gdpr", "policy update"],
    ),
    "work_or_lead": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        ["customer", "lead", "proposal", "meeting", "approval", "signature"],
    ),
}

LOW_VALUE_PATTERNS = [
    r"newsletter",
    r"unsubscribe",
    r"no-reply",
    r"market briefing",
    r"daily update",
    r"promo",
    r"deal",
    r"discount",
]


def _contains_any(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return any(term in t for term in terms)


def _default_summary(context: MessageContext) -> str:
    one_line = re.sub(r"\s+", " ", context.snippet or context.body_text).strip()
    return one_line[:160] if one_line else "No summary available"


def classify_message(
    context: MessageContext,
    approved_trash_senders: set[str],
    use_model: bool = False,
) -> ClassificationResult:
    hits = [name for name, fn in PROTECTION_PATTERNS.items() if fn(context)]
    if hits:
        return ClassificationResult(
            decision="review",
            confidence=0.99,
            reason="Protection rules triggered",
            summary=_default_summary(context),
            protection_hits=hits,
        )

    sender_l = context.sender.lower()
    low_value_score = sum(
        1 for p in LOW_VALUE_PATTERNS if re.search(p, (context.subject + " " + context.snippet).lower())
    )
    sender_approved = any(s in sender_l for s in approved_trash_senders)

    if sender_approved and low_value_score >= 2:
        result = ClassificationResult(
            decision="summarize_then_trash",
            confidence=min(0.75 + (0.05 * low_value_score), 0.98),
            reason="Approved sender and low-value signals",
            summary=_default_summary(context),
            protection_hits=[],
        )
    elif low_value_score >= 1:
        result = ClassificationResult(
            decision="review",
            confidence=0.65,
            reason="Low-value signals present but insufficient confidence",
            summary=_default_summary(context),
            protection_hits=[],
        )
    else:
        result = ClassificationResult(
            decision="keep",
            confidence=0.80,
            reason="No low-value signals",
            summary=_default_summary(context),
            protection_hits=[],
        )

    if use_model:
        return _refine_with_model(context, result)
    return result


def _refine_with_model(context: MessageContext, initial: ClassificationResult) -> ClassificationResult:
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return initial

    prompt = (
        "Return JSON with keys: decision, confidence, reason, summary. "
        "Never escalate to summarize_then_trash unless initial decision is summarize_then_trash. "
        f"Initial decision={initial.decision}. Subject={context.subject!r}. Snippet={context.snippet!r}."
    )
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "temperature": 0,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            },
            timeout=20,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()["choices"][0]["message"]["content"]
    except Exception:
        return initial

    if isinstance(data, str):
        import json

        try:
            data = json.loads(data)
        except Exception:
            return initial

    decision = data.get("decision", initial.decision)
    if initial.decision != "summarize_then_trash" and decision == "summarize_then_trash":
        decision = "review"
    return ClassificationResult(
        decision=decision if decision in {"keep", "review", "summarize_then_trash"} else initial.decision,
        confidence=float(data.get("confidence", initial.confidence)),
        reason=str(data.get("reason", initial.reason))[:180],
        summary=str(data.get("summary", initial.summary))[:180],
        protection_hits=initial.protection_hits,
    )
