from __future__ import annotations

import json
import os
import re
from email.utils import parseaddr
from typing import Any


from src.models import ClassificationResult, MessageContext


PROTECTION_PATTERNS = {
    "has_attachments": lambda m: m.has_attachments,
    "reply_thread": lambda m: m.is_reply_thread,
    "financial": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        ["invoice", "receipt", "payment", "quote", "contract", "tax", "wire", "remittance"],
    ),
    "legal_or_compliance": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        ["legal", "compliance", "regulatory", "gdpr", "policy update", "terms", "dpa"],
    ),
    "work_or_lead": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        [
            "customer",
            "lead",
            "proposal",
            "meeting",
            "approval",
            "signature",
            "deadline",
            "appointment",
        ],
    ),
    "sensitive_security": lambda m: _contains_any(
        m.subject + " " + m.body_text,
        ["2fa", "otp", "verification code", "security alert", "password reset"],
    ),
}

LOW_VALUE_PATTERNS = [
    r"newsletter",
    r"unsubscribe",
    r"no-?reply",
    r"market briefing",
    r"daily update",
    r"promo",
    r"deal",
    r"discount",
    r"weekly digest",
    r"product updates?",
]


def _contains_any(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return any(term in t for term in terms)


def _sender_email(from_header: str) -> str:
    return parseaddr(from_header)[1].lower()


def _default_summary(context: MessageContext) -> str:
    one_line = re.sub(r"\s+", " ", context.snippet or context.body_text).strip()
    if not one_line:
        one_line = context.subject.strip()
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

    sender = _sender_email(context.sender)
    searchable_text = (context.subject + " " + context.snippet).lower()
    low_value_score = sum(1 for pattern in LOW_VALUE_PATTERNS if re.search(pattern, searchable_text))

    # Exact sender match, plus optional domain lane support via @domain.tld entries.
    sender_approved = sender in approved_trash_senders or any(
        rule.startswith("@") and sender.endswith(rule) for rule in approved_trash_senders
    )

    if sender_approved and low_value_score >= 2:
        result = ClassificationResult(
            decision="summarize_then_trash",
            confidence=min(0.80 + (0.04 * low_value_score), 0.98),
            reason="Approved sender and multiple low-value signals",
            summary=_default_summary(context),
            protection_hits=[],
        )
    elif low_value_score >= 1:
        result = ClassificationResult(
            decision="review",
            confidence=0.60,
            reason="Low-value signals present but below trash confidence",
            summary=_default_summary(context),
            protection_hits=[],
        )
    else:
        result = ClassificationResult(
            decision="keep",
            confidence=0.85,
            reason="No clear low-value signals",
            summary=_default_summary(context),
            protection_hits=[],
        )

    return _refine_with_model(context, result) if use_model else result


def _refine_with_model(context: MessageContext, initial: ClassificationResult) -> ClassificationResult:
    api_key = os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return initial

    prompt = (
        "You are a conservative inbox safety classifier. Return JSON object with keys "
        "decision, confidence, reason, summary. Allowed decisions: keep|review|summarize_then_trash. "
        "Never output summarize_then_trash if initial_decision is not summarize_then_trash. "
        f"initial_decision={initial.decision}; subject={context.subject!r}; snippet={context.snippet!r}"
    )
    try:
        import requests

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
        content = response.json()["choices"][0]["message"]["content"]
        data: dict[str, Any] = json.loads(content) if isinstance(content, str) else content
    except Exception:
        return initial

    decision = str(data.get("decision", initial.decision)).strip().lower()
    if initial.decision != "summarize_then_trash" and decision == "summarize_then_trash":
        decision = "review"
    if decision not in {"keep", "review", "summarize_then_trash"}:
        decision = initial.decision

    try:
        confidence = float(data.get("confidence", initial.confidence))
    except (TypeError, ValueError):
        confidence = initial.confidence

    confidence = max(0.0, min(confidence, 1.0))
    return ClassificationResult(
        decision=decision,
        confidence=confidence,
        reason=str(data.get("reason", initial.reason))[:180],
        summary=str(data.get("summary", initial.summary))[:180],
        protection_hits=initial.protection_hits,
    )
