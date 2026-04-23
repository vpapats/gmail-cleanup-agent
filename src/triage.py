from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.audit import AuditLogger
from src.classifier import classify_message
from src.gmail_client import GmailClient
from src.models import AuditRecord, ClassificationResult, MessageContext


@dataclass
class TriageConfig:
    mode: str
    use_model: bool
    min_trash_confidence: float
    max_messages_per_run: int
    approved_trash_senders: set[str]
    candidate_queries: list[str]
    labels: dict[str, str]


class TriageRunner:
    def __init__(self, config: TriageConfig, audit_dir: Path) -> None:
        self.config = config
        self.gmail = GmailClient()
        self.audit = AuditLogger(audit_dir)
        self.label_ids = {k: self.gmail.ensure_label(v) for k, v in config.labels.items()}

    def run(self) -> dict[str, int]:
        counters = {"keep": 0, "review": 0, "summarize_then_trash": 0, "trashed": 0, "errors": 0}
        candidate_ids = self._collect_candidates()[: self.config.max_messages_per_run]

        for message_id in candidate_ids:
            try:
                context = self.gmail.get_message_context(message_id)
                result = classify_message(
                    context,
                    approved_trash_senders=self.config.approved_trash_senders,
                    use_model=self.config.use_model,
                )
                action_taken = self._apply_decision(context, result)
                self.audit.log(AuditRecord.create(context, result, action_taken=action_taken))
                counters[result.decision] += 1
                if action_taken == "trashed":
                    counters["trashed"] += 1
            except Exception as err:
                counters["errors"] += 1
                fallback_context = MessageContext(
                    message_id=message_id,
                    thread_id="",
                    sender="",
                    subject="",
                    snippet="",
                    body_text="",
                    has_attachments=False,
                    is_reply_thread=False,
                )
                fallback_result = ClassificationResult(
                    decision="review",
                    confidence=0.0,
                    reason="processing_error",
                    summary="",
                    protection_hits=[],
                )
                self.audit.log(
                    AuditRecord.create(
                        fallback_context,
                        fallback_result,
                        action_taken="error",
                        error=str(err),
                    )
                )

        return counters

    def _collect_candidates(self) -> list[str]:
        ids: list[str] = []
        for query in self.config.candidate_queries:
            ids.extend(self.gmail.list_candidates(query))
        return list(dict.fromkeys(ids))

    def _apply_decision(self, context: MessageContext, result: ClassificationResult) -> str:
        if result.decision == "keep":
            self.gmail.add_label(context.message_id, self.label_ids["kept"])
            return "labeled_kept"

        if result.decision == "review":
            if result.protection_hits:
                self.gmail.add_label(context.message_id, self.label_ids["protected"])
                return "labeled_protected"
            self.gmail.add_label(context.message_id, self.label_ids["review"])
            return "labeled_review"

        self.gmail.add_label(context.message_id, self.label_ids["trash_after_summary"])
        if self.config.mode == "active" and result.confidence >= self.config.min_trash_confidence:
            self.gmail.trash_message(context.message_id)
            return "trashed"
        return "shadow_no_delete"
