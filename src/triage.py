from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
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
        counters = {
            "important": 0,
            "action_needed": 0,
            "low_priority": 0,
            "review": 0,
            "trashed": 0,
            "restored": 0,
            "errors": 0,
        }
        protected_senders, feedback_ids, restored = self._process_feedback()
        counters["restored"] = restored
        candidate_ids = [
            message_id for message_id in self._collect_candidates() if message_id not in feedback_ids
        ][: self.config.max_messages_per_run]

        for message_id in candidate_ids:
            try:
                context = self.gmail.get_message_context(message_id)
                result = classify_message(
                    context,
                    approved_trash_senders=self.config.approved_trash_senders,
                    protected_senders=protected_senders,
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

    def _process_feedback(self) -> tuple[set[str], set[str], int]:
        feedback_label = self.config.labels["wrongly_trashed"]
        feedback_ids = set(self.gmail.list_candidates(f"label:{feedback_label}", max_messages=500))
        protected_senders: set[str] = set()
        restored = 0

        for message_id in feedback_ids:
            context = self.gmail.get_message_context(message_id)
            sender_address = parseaddr(context.sender)[1].lower()
            if sender_address:
                protected_senders.add(sender_address)

            if "TRASH" in context.labels:
                self.gmail.untrash_message(message_id)
                self.gmail.add_label(message_id, "INBOX")
                restored += 1

            self.gmail.remove_label(message_id, self.label_ids["low_priority"])
            self.gmail.add_label(message_id, self.label_ids["important"])

            result = ClassificationResult(
                decision="important",
                confidence=1.0,
                reason="Restored or protected by user feedback",
                summary=context.snippet[:180],
                protection_hits=["user_feedback"],
            )
            self.audit.log(AuditRecord.create(context, result, action_taken="feedback_protected"))

        return protected_senders, feedback_ids, restored

    def _collect_candidates(self) -> list[str]:
        ids: list[str] = []
        for query in self.config.candidate_queries:
            ids.extend(self.gmail.list_candidates(query, max_messages=self.config.max_messages_per_run))
        return list(dict.fromkeys(ids))

    def _apply_decision(self, context: MessageContext, result: ClassificationResult) -> str:
        if result.decision == "important":
            self.gmail.add_label(context.message_id, self.label_ids["important"])
            return "labeled_important"

        if result.decision == "action_needed":
            self.gmail.add_label(context.message_id, self.label_ids["action_needed"])
            return "labeled_action_needed"

        if result.decision == "review":
            self.gmail.add_label(context.message_id, self.label_ids["review"])
            return "labeled_review"

        self.gmail.add_label(context.message_id, self.label_ids["low_priority"])
        if self.config.mode == "active" and result.confidence >= self.config.min_trash_confidence:
            self.gmail.trash_message(context.message_id)
            return "trashed"
        return "shadow_no_delete"
