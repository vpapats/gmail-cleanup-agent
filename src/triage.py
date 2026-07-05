from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.utils import parseaddr
from pathlib import Path

from src.audit import AuditLogger
from src.classifier import classify_message
from src.digest import DigestItem, build_daily_summary, summarize_for_digest
from src.gmail_client import GmailClient
from src.models import AuditRecord, ClassificationResult, MessageContext


@dataclass
class DailySummaryConfig:
    enabled: bool
    decisions: set[str]
    trash_after_send: bool
    send_when_empty: bool
    subject_prefix: str


@dataclass
class TriageConfig:
    mode: str
    use_model: bool
    min_trash_confidence: float
    max_messages_per_run: int
    recent_messages_per_run: int
    candidate_scan_limit: int
    approved_trash_senders: set[str]
    candidate_queries: list[str]
    labels: dict[str, str]
    daily_summary: DailySummaryConfig


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
            "summarized": 0,
            "summary_sent": 0,
            "restored": 0,
            "errors": 0,
        }
        digest_items: list[DigestItem] = []
        protected_senders, feedback_ids, restored = self._process_feedback()
        counters["restored"] = restored
        pending_items, pending_ids, pending_errors = self._collect_pending_digest_items(feedback_ids)
        digest_items.extend(pending_items)
        counters["errors"] += pending_errors
        for item in pending_items:
            counters[item.result.decision] += 1
        remaining_messages = max(0, self.config.max_messages_per_run - len(pending_ids))
        candidate_ids = [
            message_id
            for message_id in self._collect_candidates()
            if message_id not in feedback_ids and message_id not in pending_ids
        ][:remaining_messages]

        for message_id in candidate_ids:
            try:
                context = self.gmail.get_message_context(message_id)
                result = classify_message(
                    context,
                    approved_trash_senders=self.config.approved_trash_senders,
                    protected_senders=protected_senders,
                    use_model=self.config.use_model,
                )
                result = self._protect_starred_result(context, result)
                action_taken = self._apply_decision(context, result)
                if self._should_digest(result):
                    digest_items.append(
                        DigestItem(
                            context=context,
                            result=result,
                            bullets=summarize_for_digest(context, result),
                        )
                    )
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

        try:
            summary_stats = self._send_daily_summary(digest_items)
            counters["summarized"] = summary_stats["summarized"]
            counters["summary_sent"] = summary_stats["summary_sent"]
            counters["trashed"] += summary_stats["trashed"]
        except Exception as err:
            counters["errors"] += 1
            fallback_context = MessageContext(
                message_id="",
                thread_id="",
                sender="GMAIL FOMO",
                subject="Daily summary",
                snippet="",
                body_text="",
                has_attachments=False,
                is_reply_thread=False,
            )
            fallback_result = ClassificationResult(
                decision="review",
                confidence=0.0,
                reason="daily_summary_error",
                summary="",
                protection_hits=[],
            )
            self.audit.log(
                AuditRecord.create(
                    fallback_context,
                    fallback_result,
                    action_taken="summary_error",
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

            for label_key in ("low_priority", "review", "daily_summary"):
                if label_key in self.label_ids:
                    self.gmail.remove_label(message_id, self.label_ids[label_key])
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
        scan_limit = max(self.config.max_messages_per_run, self.config.candidate_scan_limit)
        for query in self.config.candidate_queries:
            ids.extend(self.gmail.list_candidates(query, max_messages=scan_limit))
        unique_ids = list(dict.fromkeys(ids))
        recent_count = min(self.config.recent_messages_per_run, self.config.max_messages_per_run)
        recent_ids = unique_ids[:recent_count]
        backlog_ids = list(reversed(unique_ids[recent_count:]))
        return list(dict.fromkeys([*recent_ids, *backlog_ids]))

    def _collect_pending_digest_items(
        self, excluded_ids: set[str]
    ) -> tuple[list[DigestItem], set[str], int]:
        if not self.config.daily_summary.enabled:
            return [], set(), 0

        summary_label = self.config.labels.get("daily_summary")
        feedback_label = self.config.labels.get("wrongly_trashed")
        decision_labels = [
            ("review", "review"),
            ("low_priority", "low_priority"),
        ]
        ids_by_decision: list[tuple[str, str]] = []
        for decision, label_key in decision_labels:
            if decision not in self.config.daily_summary.decisions:
                continue
            label = self.config.labels.get(label_key)
            if not label:
                continue
            query_parts = [f"in:anywhere label:{label}"]
            if summary_label:
                query_parts.append(f"-label:{summary_label}")
            if feedback_label:
                query_parts.append(f"-label:{feedback_label}")
            query = " ".join(query_parts)
            ids_by_decision.extend(
                (message_id, decision)
                for message_id in self.gmail.list_candidates(
                    query,
                    max_messages=self.config.max_messages_per_run,
                )
            )

        items: list[DigestItem] = []
        collected_ids: set[str] = set()
        errors = 0
        for message_id, decision in ids_by_decision:
            if message_id in excluded_ids or message_id in collected_ids:
                continue
            if len(collected_ids) >= self.config.max_messages_per_run:
                break
            try:
                context = self.gmail.get_message_context(message_id)
                result = ClassificationResult(
                    decision=decision,
                    confidence=1.0,
                    reason="Existing AI label pending daily summary",
                    summary=context.snippet[:180],
                    protection_hits=[],
                )
                items.append(
                    DigestItem(
                        context=context,
                        result=result,
                        bullets=summarize_for_digest(context, result),
                    )
                )
                collected_ids.add(message_id)
                self.audit.log(
                    AuditRecord.create(
                        context,
                        result,
                        action_taken="queued_existing_for_daily_summary",
                    )
                )
            except Exception as err:
                errors += 1
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
                    decision=decision,
                    confidence=0.0,
                    reason="pending_digest_error",
                    summary="",
                    protection_hits=[],
                )
                self.audit.log(
                    AuditRecord.create(
                        fallback_context,
                        fallback_result,
                        action_taken="pending_digest_error",
                        error=str(err),
                    )
                )

        return items, collected_ids, errors

    def _apply_decision(self, context: MessageContext, result: ClassificationResult) -> str:
        if self._is_starred(context):
            self.gmail.add_label(context.message_id, self.label_ids["important"])
            return "protected_starred"

        if result.decision == "important":
            self.gmail.add_label(context.message_id, self.label_ids["important"])
            return "labeled_important"

        if result.decision == "action_needed":
            self.gmail.add_label(context.message_id, self.label_ids["action_needed"])
            return "labeled_action_needed"

        if result.decision == "review":
            self.gmail.add_label(context.message_id, self.label_ids["review"])
            if self._should_digest(result):
                return "queued_for_daily_summary"
            return "labeled_review"

        self.gmail.add_label(context.message_id, self.label_ids["low_priority"])
        if self._should_digest(result):
            return "queued_for_daily_summary"
        if self.config.mode == "active" and result.confidence >= self.config.min_trash_confidence:
            self.gmail.trash_message(context.message_id)
            return "trashed"
        return "shadow_no_delete"

    def _should_digest(self, result: ClassificationResult) -> bool:
        return self.config.daily_summary.enabled and result.decision in self.config.daily_summary.decisions

    def _is_starred(self, context: MessageContext) -> bool:
        return "STARRED" in context.labels

    def _protect_starred_result(
        self, context: MessageContext, result: ClassificationResult
    ) -> ClassificationResult:
        if not self._is_starred(context):
            return result

        protection_hits = list(dict.fromkeys([*result.protection_hits, "starred"]))
        return ClassificationResult(
            decision="important",
            confidence=1.0,
            reason="Message is starred in Gmail",
            summary=result.summary or context.snippet[:180],
            protection_hits=protection_hits,
        )

    def _send_daily_summary(self, items: list[DigestItem]) -> dict[str, int]:
        stats = {"summarized": 0, "summary_sent": 0, "trashed": 0}
        if not self.config.daily_summary.enabled:
            return stats
        if not items and not self.config.daily_summary.send_when_empty:
            return stats

        recipient = self.gmail.get_profile_email()
        subject = f"{self.config.daily_summary.subject_prefix} - {date.today().isoformat()}"
        body = build_daily_summary(items, date.today())
        self.gmail.send_email(recipient, subject, body)
        stats["summary_sent"] = 1

        for item in items:
            if self._is_starred(item.context):
                self.gmail.add_label(item.context.message_id, self.label_ids["important"])
                self.audit.log(
                    AuditRecord.create(
                        item.context,
                        self._protect_starred_result(item.context, item.result),
                        action_taken="protected_starred",
                    )
                )
                continue

            self.gmail.add_label(item.context.message_id, self.label_ids["daily_summary"])
            stats["summarized"] += 1
            if self.config.mode == "active" and self.config.daily_summary.trash_after_send:
                self.gmail.trash_message(item.context.message_id)
                stats["trashed"] += 1
                self.audit.log(
                    AuditRecord.create(
                        item.context,
                        item.result,
                        action_taken="summarized_and_trashed",
                    )
                )

        return stats
