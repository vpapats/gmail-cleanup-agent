from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from src.audit import AuditLogger
from src.classifier import classify_message
from src.digest import DigestItem, build_daily_summary, summarize_for_digest
from src.feedback import (
    FeedbackExample,
    FeedbackReview,
    build_feedback_example,
    review_wrongly_trashed,
    select_relevant_feedback_examples,
)
from src.gmail_client import GmailClient
from src.github_state import GitHubFeedbackStateStore
from src.models import (
    AuditRecord,
    ClassificationResult,
    HistoricalEmailReview,
    HistoricalReceivedDateMatch,
    MessageContext,
)


ATHENS = ZoneInfo("Europe/Athens")
HISTORICAL_MIN_REVIEW_CONFIDENCE = 0.80


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
    candidate_scan_limit: int | None
    approved_trash_senders: set[str]
    candidate_queries: list[str]
    labels: dict[str, str]
    daily_summary: DailySummaryConfig


class TriageRunner:
    def __init__(
        self,
        config: TriageConfig,
        audit_dir: Path,
        *,
        gmail: GmailClient | None = None,
        feedback_state: GitHubFeedbackStateStore | None = None,
    ) -> None:
        self.config = config
        self.gmail = gmail or GmailClient()
        self.feedback_state = feedback_state or GitHubFeedbackStateStore.from_env()
        self.audit = AuditLogger(audit_dir)
        self.label_ids = {k: self.gmail.ensure_label(v) for k, v in config.labels.items()}

    def run(self) -> dict[str, int]:
        counters = {
            "kept": 0,
            "action_needed": 0,
            "digest_and_trash": 0,
            "trashed": 0,
            "summarized": 0,
            "summary_sent": 0,
            "restored": 0,
            "feedback_reviewed": 0,
            "historical_reviewed": 0,
            "historical_relabelled": 0,
            "historical_deferred": 0,
            "errors": 0,
        }
        digest_items: list[DigestItem] = []
        (
            feedback_examples,
            feedback_history_ids,
            example_errors,
        ) = self._load_feedback_examples()
        feedback_reviews, feedback_ids, restored, feedback_errors = self._process_feedback(
            feedback_examples,
        )
        feedback_examples.extend(
            build_feedback_example(review.context) for review in feedback_reviews
        )
        counters["restored"] = restored
        counters["errors"] += example_errors + feedback_errors

        historical_target_date: date | None = None
        historical_matches: list[HistoricalReceivedDateMatch] = []
        historical_ids: set[str] = set()
        find_matches = getattr(
            self.gmail,
            "list_previous_year_received_date_matches",
            None,
        )
        if self.config.daily_summary.enabled and callable(find_matches):
            historical_target_date = _athens_today()
            try:
                historical_matches = find_matches(
                    historical_target_date,
                    excluded_message_ids=(
                        feedback_history_ids | feedback_ids
                    ),
                )
                for match in historical_matches:
                    historical_ids.update((match.message_id, *match.message_ids))
            except Exception as err:
                counters["errors"] += 1
                self._log_historical_lookup_error(err)
                return counters

        pending_items, pending_ids, pending_errors = self._collect_pending_digest_items(
            feedback_ids | historical_ids
        )
        digest_items.extend(pending_items)
        counters["errors"] += pending_errors
        for item in pending_items:
            counters[item.result.decision] += 1
        remaining_messages = max(0, self.config.max_messages_per_run - len(pending_ids))
        candidate_ids = [
            message_id
            for message_id in self._collect_candidates()
            if message_id not in feedback_ids
            and message_id not in pending_ids
            and message_id not in historical_ids
        ][:remaining_messages]

        for message_id in candidate_ids:
            try:
                context = self.gmail.get_message_context(message_id)
                result = classify_message(
                    context,
                    approved_trash_senders=self.config.approved_trash_senders,
                    feedback_examples=feedback_examples,
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
                    decision="digest_and_trash",
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
            summary_stats = self._send_daily_summary(
                digest_items,
                feedback_reviews,
                feedback_history_ids=feedback_history_ids,
                historical_target_date=historical_target_date,
                historical_matches=historical_matches,
                feedback_examples=feedback_examples,
            )
            counters["summarized"] = summary_stats["summarized"]
            counters["summary_sent"] = summary_stats["summary_sent"]
            counters["trashed"] += summary_stats["trashed"]
            counters["feedback_reviewed"] = summary_stats["feedback_reviewed"]
            counters["historical_reviewed"] = summary_stats["historical_reviewed"]
            counters["historical_relabelled"] = summary_stats["historical_relabelled"]
            counters["historical_deferred"] = summary_stats["historical_deferred"]
            counters["errors"] += summary_stats["historical_errors"]
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
                decision="digest_and_trash",
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

    def _load_feedback_examples(
        self,
    ) -> tuple[list[FeedbackExample], set[str], int]:
        examples: list[FeedbackExample] = []
        errors = 0
        example_ids = self.feedback_state.load()
        missing_ids: list[str] = []
        for message_id in example_ids:
            try:
                examples.append(build_feedback_example(self.gmail.get_message_context(message_id)))
            except HttpError as err:
                if getattr(getattr(err, "resp", None), "status", None) == 404:
                    try:
                        if self.gmail.is_message_missing(message_id):
                            missing_ids.append(message_id)
                            continue
                    except Exception as check_err:
                        errors += 1
                        self._log_feedback_error(
                            message_id,
                            "feedback_example_error",
                            RuntimeError(
                                f"Could not confirm whether feedback message is missing: {check_err}"
                            ),
                        )
                        continue
                errors += 1
                self._log_feedback_error(message_id, "feedback_example_error", err)
            except Exception as err:
                errors += 1
                self._log_feedback_error(message_id, "feedback_example_error", err)

        history_ids = set(example_ids)
        if missing_ids:
            try:
                self.feedback_state.remove_missing_message_ids(missing_ids)
            except Exception as err:
                errors += 1
                for message_id in missing_ids:
                    self._log_feedback_error(
                        message_id,
                        "feedback_state_prune_error",
                        err,
                    )
            else:
                history_ids.difference_update(missing_ids)
                for message_id in missing_ids:
                    self._log_feedback_example_pruned(message_id)
        return examples, history_ids, errors

    def _process_feedback(
        self,
        feedback_examples: list[FeedbackExample],
        *,
        excluded_ids: set[str] | None = None,
    ) -> tuple[list[FeedbackReview], set[str], int, int]:
        feedback_label = self.config.labels["wrongly_trashed"]
        excluded_ids = excluded_ids or set()
        query = f"in:anywhere label:{feedback_label}"
        feedback_id_list = [
            message_id
            for message_id in dict.fromkeys(
                self.gmail.list_candidates(query, max_messages=500)
            )
            if message_id not in excluded_ids
        ]
        feedback_ids = set(feedback_id_list)
        reviews: list[FeedbackReview] = []
        restored = 0
        errors = 0

        for message_id in feedback_id_list:
            try:
                context = self.gmail.get_message_context(message_id)
                if "TRASH" in context.labels:
                    self.gmail.untrash_message(message_id)
                    self.gmail.add_label(message_id, "INBOX")
                    restored += 1

                related_examples = select_relevant_feedback_examples(
                    context,
                    feedback_examples,
                )
                review = review_wrongly_trashed(
                    context,
                    related_examples,
                    use_model=self.config.use_model,
                )
                reviews.append(review)
                result = ClassificationResult(
                    decision="kept",
                    confidence=1.0,
                    reason=review.reason[:180],
                    summary=context.snippet[:180],
                    protection_hits=["user_feedback"],
                )
                self.audit.log(
                    AuditRecord.create(
                        context,
                        result,
                        action_taken="feedback_restored_pending_summary",
                    )
                )
            except Exception as err:
                errors += 1
                self._log_feedback_error(message_id, "feedback_review_error", err)

        return reviews, feedback_ids, restored, errors

    def _log_feedback_error(self, message_id: str, action: str, err: Exception) -> None:
        context = MessageContext(
            message_id=message_id,
            thread_id="",
            sender="",
            subject="",
            snippet="",
            body_text="",
            has_attachments=False,
            is_reply_thread=False,
        )
        result = ClassificationResult(
            decision="kept",
            confidence=0.0,
            reason=action,
            summary="",
            protection_hits=["user_feedback"],
        )
        self.audit.log(
            AuditRecord.create(
                context,
                result,
                action_taken=action,
                error=str(err),
            )
        )

    def _log_feedback_example_pruned(self, message_id: str) -> None:
        context = MessageContext(
            message_id=message_id,
            thread_id="",
            sender="",
            subject="",
            snippet="",
            body_text="",
            has_attachments=False,
            is_reply_thread=False,
        )
        result = ClassificationResult(
            decision="kept",
            confidence=1.0,
            reason="stale_feedback_example_pruned",
            summary="",
            protection_hits=[],
        )
        self.audit.log(
            AuditRecord.create(
                context,
                result,
                action_taken="feedback_example_pruned",
            )
        )

    def _collect_candidates(self) -> list[str]:
        ids: list[str] = []
        scan_limit = self.config.candidate_scan_limit
        if scan_limit is not None:
            scan_limit = max(self.config.max_messages_per_run, scan_limit)
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
        decision_labels = [("digest_and_trash", "digest_and_trash")]
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
            self._set_decision_label(context.message_id, "kept")
            return "protected_starred"

        if result.decision == "kept":
            self._set_decision_label(context.message_id, "kept")
            return "labeled_kept"

        if result.decision == "action_needed":
            self._set_decision_label(context.message_id, "action_needed")
            return "labeled_action_needed"

        if result.confidence < self.config.min_trash_confidence:
            return "deferred_low_confidence"

        self._set_decision_label(context.message_id, "digest_and_trash")
        if self._should_digest(result):
            return "queued_for_daily_summary"
        if self.config.mode == "active" and result.confidence >= self.config.min_trash_confidence:
            self.gmail.trash_message(context.message_id)
            return "trashed"
        return "shadow_no_delete"

    def _set_decision_label(self, message_id: str, decision: str) -> None:
        for label_key in ("kept", "action_needed", "digest_and_trash"):
            if label_key != decision:
                self.gmail.remove_label(message_id, self.label_ids[label_key])
        self.gmail.add_label(message_id, self.label_ids[decision])

    def _decision_label_ids(self, labels: set[str] | list[str]) -> tuple[str, ...]:
        return tuple(
            sorted(
                self.label_ids[key]
                for key in ("kept", "action_needed", "digest_and_trash")
                if key in self.label_ids and self.label_ids[key] in labels
            )
        )

    def _decision_label_name(self, label_ids: tuple[str, ...]) -> str:
        names = {
            self.label_ids[key]: self.config.labels.get(key, key)
            for key in ("kept", "action_needed", "digest_and_trash")
            if key in self.label_ids
        }
        if not label_ids:
            return "Unlabeled"
        return " + ".join(sorted(names.get(label_id, "Unknown label") for label_id in label_ids))

    def _review_historical_matches(
        self,
        target_date: date,
        matches: list[HistoricalReceivedDateMatch],
        feedback_examples: list[FeedbackExample],
    ) -> list[HistoricalEmailReview]:
        reviews: list[HistoricalEmailReview] = []
        for match in matches:
            message_ids = tuple(dict.fromkeys(match.message_ids or (match.message_id,)))
            for message_id in message_ids:
                context: MessageContext | None = None
                try:
                    context = self.gmail.get_message_context(message_id)
                    received_date = datetime.fromisoformat(
                        context.received_at.replace("Z", "+00:00")
                    ).astimezone(ATHENS).date()
                    expected_date = date(match.year, target_date.month, target_date.day)
                    if received_date != expected_date:
                        raise ValueError(
                            "Gmail internalDate does not match the historical Athens date"
                        )

                    result = classify_message(
                        context,
                        approved_trash_senders=self.config.approved_trash_senders,
                        feedback_examples=feedback_examples,
                        use_model=self.config.use_model,
                    )
                    result = self._protect_starred_result(context, result)
                    original_label_ids = self._decision_label_ids(context.labels)
                    previous_label = self._decision_label_name(original_label_ids)
                    proposed_label = self.config.labels.get(
                        result.decision, result.decision
                    )

                    if (
                        result.decision == "digest_and_trash"
                        and result.confidence < self.config.min_trash_confidence
                    ):
                        status = "deferred"
                        reason = "below the configured digest confidence threshold"
                    elif result.confidence < HISTORICAL_MIN_REVIEW_CONFIDENCE:
                        status = "deferred"
                        reason = "below the safe historical relabel confidence threshold"
                    else:
                        target_label_id = self.label_ids.get(result.decision)
                        if not target_label_id:
                            raise ValueError(
                                f"No Gmail label is configured for {result.decision}"
                            )
                        status = (
                            "unchanged"
                            if original_label_ids == (target_label_id,)
                            else "change"
                        )
                        reason = result.reason

                    review = HistoricalEmailReview(
                        message_id=message_id,
                        year=match.year,
                        sender=context.sender,
                        subject=" ".join(context.subject.split())[:500]
                        or "(no subject)",
                        received_at=context.received_at,
                        previous_label=previous_label,
                        proposed_label=proposed_label,
                        confidence=result.confidence,
                        status=status,
                        reason=reason,
                        context=context,
                        result=result,
                        original_decision_label_ids=original_label_ids,
                    )
                    reviews.append(review)
                    action = {
                        "change": "historical_relabel_planned",
                        "unchanged": "historical_review_unchanged",
                        "deferred": "historical_review_deferred",
                    }[status]
                    self.audit.log(AuditRecord.create(context, result, action_taken=action))
                except Exception as err:
                    fallback_context = context or MessageContext(
                        message_id=message_id,
                        thread_id="",
                        sender=match.sender if message_id == match.message_id else "",
                        subject=match.subject if message_id == match.message_id else "",
                        snippet="",
                        body_text="",
                        has_attachments=False,
                        is_reply_thread=False,
                        received_at=datetime(
                            match.year,
                            target_date.month,
                            target_date.day,
                            tzinfo=ATHENS,
                        ).isoformat(),
                    )
                    fallback_result = ClassificationResult(
                        decision="kept",
                        confidence=0.0,
                        reason="historical_review_error",
                        summary="",
                        protection_hits=[],
                    )
                    review = HistoricalEmailReview(
                        message_id=message_id,
                        year=match.year,
                        sender=fallback_context.sender,
                        subject=fallback_context.subject or "(unavailable)",
                        received_at=fallback_context.received_at,
                        previous_label="Unknown",
                        proposed_label="Not changed",
                        confidence=0.0,
                        status="error",
                        reason=" ".join(str(err).split())[:180],
                        context=fallback_context,
                        result=fallback_result,
                    )
                    reviews.append(review)
                    self.audit.log(
                        AuditRecord.create(
                            fallback_context,
                            fallback_result,
                            action_taken="historical_review_error",
                            error=str(err),
                        )
                    )
        return reviews

    def _apply_historical_reviews(
        self,
        reviews: list[HistoricalEmailReview],
    ) -> tuple[int, int]:
        relabelled = 0
        errors = 0
        get_labels = getattr(self.gmail, "get_message_labels", None)
        for review in reviews:
            if review.status not in {"change", "unchanged"}:
                continue
            context = review.context
            result = review.result
            if context is None or result is None:
                errors += 1
                continue
            try:
                current_labels = set(
                    get_labels(review.message_id)
                    if callable(get_labels)
                    else context.labels
                )
                current_decision_ids = self._decision_label_ids(current_labels)
                if current_decision_ids != review.original_decision_label_ids:
                    raise RuntimeError(
                        "Gmail category labels changed while the historical review was pending"
                    )

                if result.decision == "digest_and_trash":
                    summary_label_id = self.label_ids.get("daily_summary")
                    if not summary_label_id:
                        raise RuntimeError(
                            "Cannot safely label historical digest without the FOMO summary marker"
                        )
                    if summary_label_id not in current_labels:
                        # This marker prevents the historical digest label from becoming a
                        # pending item that a later run might move to Trash.
                        self.gmail.add_label(review.message_id, summary_label_id)

                target_label_id = self.label_ids.get(result.decision)
                if not target_label_id:
                    raise RuntimeError(
                        f"No Gmail label is configured for {result.decision}"
                    )
                if current_decision_ids != (target_label_id,):
                    self._set_decision_label(review.message_id, result.decision)
                    relabelled += 1
                    action = "historical_review_relabelled_no_trash"
                elif result.decision == "digest_and_trash" and (
                    self.label_ids["daily_summary"] not in current_labels
                ):
                    action = "historical_review_marked_summarized_no_trash"
                else:
                    action = "historical_review_unchanged"
                self.audit.log(AuditRecord.create(context, result, action_taken=action))
            except Exception as err:
                errors += 1
                self.audit.log(
                    AuditRecord.create(
                        context,
                        result,
                        action_taken="historical_review_apply_error",
                        error=str(err),
                    )
                )
        return relabelled, errors

    def _log_historical_lookup_error(self, err: Exception) -> None:
        context = MessageContext(
            message_id="",
            thread_id="",
            sender="GMAIL FOMO",
            subject="Historical received-date review",
            snippet="",
            body_text="",
            has_attachments=False,
            is_reply_thread=False,
        )
        result = ClassificationResult(
            decision="kept",
            confidence=0.0,
            reason="historical_lookup_error",
            summary="",
            protection_hits=[],
        )
        self.audit.log(
            AuditRecord.create(
                context,
                result,
                action_taken="historical_lookup_error",
                error=str(err),
            )
        )

    def _should_digest(self, result: ClassificationResult) -> bool:
        return (
            self.config.daily_summary.enabled
            and result.decision in self.config.daily_summary.decisions
            and result.confidence >= self.config.min_trash_confidence
        )

    def _is_starred(self, context: MessageContext) -> bool:
        return "STARRED" in context.labels

    def _protect_starred_result(
        self, context: MessageContext, result: ClassificationResult
    ) -> ClassificationResult:
        if not self._is_starred(context):
            return result

        protection_hits = list(dict.fromkeys([*result.protection_hits, "starred"]))
        return ClassificationResult(
            decision="kept",
            confidence=1.0,
            reason="Message is starred in Gmail",
            summary=result.summary or context.snippet[:180],
            protection_hits=protection_hits,
        )

    def _send_daily_summary(
        self,
        items: list[DigestItem],
        feedback_reviews: list[FeedbackReview] | None = None,
        *,
        feedback_history_ids: set[str] | None = None,
        historical_target_date: date | None = None,
        historical_matches: list[HistoricalReceivedDateMatch] | None = None,
        feedback_examples: list[FeedbackExample] | None = None,
    ) -> dict[str, int]:
        feedback_reviews = feedback_reviews or []
        feedback_history_ids = feedback_history_ids or set()
        historical_matches = historical_matches or []
        feedback_examples = feedback_examples or []
        stats = {
            "summarized": 0,
            "summary_sent": 0,
            "trashed": 0,
            "feedback_reviewed": 0,
            "historical_reviewed": 0,
            "historical_relabelled": 0,
            "historical_deferred": 0,
            "historical_errors": 0,
        }
        if not self.config.daily_summary.enabled:
            return stats
        if (
            not items
            and not feedback_reviews
            and not historical_matches
            and not self.config.daily_summary.send_when_empty
        ):
            return stats

        recipient = self.gmail.get_profile_email()
        summary_date = historical_target_date or _athens_today()
        subject = f"{self.config.daily_summary.subject_prefix} - {summary_date.isoformat()}"
        historical_reviews = (
            self._review_historical_matches(
                historical_target_date,
                historical_matches,
                feedback_examples,
            )
            if historical_target_date is not None
            else []
        )
        stats["historical_reviewed"] = sum(
            review.status in {"change", "unchanged", "deferred"}
            for review in historical_reviews
        )
        stats["historical_deferred"] = sum(
            review.status == "deferred" for review in historical_reviews
        )
        stats["historical_errors"] = sum(
            review.status == "error" for review in historical_reviews
        )
        body = build_daily_summary(
            items,
            summary_date,
            feedback_reviews,
            historical_target_date=historical_target_date,
            historical_matches=historical_matches,
            historical_reviews=historical_reviews,
        )
        historical_review_signature = None
        if historical_target_date is not None:
            historical_review_signature = [
                "|".join(
                    [
                        review.message_id,
                        (
                            "eligible"
                            if review.status in {"change", "unchanged"}
                            else review.status
                        ),
                        review.proposed_label,
                        f"{review.confidence:.2f}",
                    ]
                )
                for review in historical_reviews
            ]
        message_id_header = _daily_summary_message_id(
            summary_date,
            [item.context.message_id for item in items],
            [review.context.message_id for review in feedback_reviews],
            historical_review_signature=historical_review_signature,
        )
        if not self.gmail.message_exists_by_rfc822_message_id(message_id_header):
            self.gmail.send_email(
                recipient,
                subject,
                body,
                message_id_header=message_id_header,
            )
            stats["summary_sent"] = 1

        relabelled, historical_apply_errors = self._apply_historical_reviews(
            historical_reviews
        )
        stats["historical_relabelled"] = relabelled
        stats["historical_errors"] += historical_apply_errors

        updated_feedback_history_ids = list(
            dict.fromkeys(
                [
                    *sorted(feedback_history_ids),
                    *(review.context.message_id for review in feedback_reviews),
                ]
            )
        )
        if feedback_reviews:
            self.feedback_state.save(updated_feedback_history_ids)

        for review in feedback_reviews:
            if "daily_summary" in self.label_ids:
                self.gmail.remove_label(
                    review.context.message_id,
                    self.label_ids["daily_summary"],
                )
            self._set_decision_label(review.context.message_id, "kept")
            self.gmail.remove_label(
                review.context.message_id,
                self.label_ids["wrongly_trashed"],
            )
            result = ClassificationResult(
                decision="kept",
                confidence=1.0,
                reason=review.reason[:180],
                summary=review.context.snippet[:180],
                protection_hits=["user_feedback"],
            )
            self.audit.log(
                AuditRecord.create(
                    review.context,
                    result,
                    action_taken="feedback_reviewed_and_kept",
                )
            )
            stats["feedback_reviewed"] += 1

        for item in items:
            if self._is_starred(item.context):
                self._set_decision_label(item.context.message_id, "kept")
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


def _athens_today(now: datetime | None = None) -> date:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    return current.astimezone(ATHENS).date()


def _daily_summary_message_id(
    summary_date: date,
    item_ids: list[str],
    feedback_ids: list[str],
    *,
    historical_review_signature: list[str] | None = None,
) -> str:
    parts = [summary_date.isoformat(), *sorted(item_ids), "feedback", *sorted(feedback_ids)]
    if historical_review_signature is not None:
        parts.extend(["historical-review-v1", *sorted(historical_review_signature)])
    material = "|".join(parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"<gmail-fomo-daily-{summary_date.isoformat()}-{digest}@gmail-fomo.local>"
