from types import SimpleNamespace

from src.models import ClassificationResult, MessageContext
from src.triage import TriageRunner


class _Gmail:
    def __init__(self, context=None, candidate_ids=None):
        self.context = context
        self.candidate_ids = candidate_ids or ["m1"]
        self.calls = []

    def add_label(self, message_id, label_id):
        self.calls.append(("add", message_id, label_id))

    def remove_label(self, message_id, label_id):
        self.calls.append(("remove", message_id, label_id))

    def trash_message(self, message_id):
        self.calls.append(("trash", message_id))

    def untrash_message(self, message_id):
        self.calls.append(("untrash", message_id))

    def list_candidates(self, query, max_messages):
        self.calls.append(("list", query, max_messages))
        return self.candidate_ids[:max_messages]

    def get_message_context(self, message_id):
        return self.context

    def get_profile_email(self):
        self.calls.append(("profile",))
        return "me@example.com"

    def send_email(self, to_address, subject, body_text):
        self.calls.append(("send", to_address, subject, body_text))
        return "sent-1"


class _Audit:
    def __init__(self):
        self.records = []

    def log(self, record):
        self.records.append(record)


def _context(labels=None):
    return MessageContext(
        message_id="m1",
        thread_id="t1",
        sender="Sender <sender@example.com>",
        subject="Subject",
        snippet="Snippet",
        body_text="Body",
        has_attachments=False,
        is_reply_thread=False,
        labels=labels or [],
    )


def _runner(context=None, daily_summary_enabled=False):
    runner = TriageRunner.__new__(TriageRunner)
    runner.gmail = _Gmail(context)
    runner.audit = _Audit()
    runner.label_ids = {
        "kept": "kept-id",
        "action_needed": "action-id",
        "digest_and_trash": "digest-id",
        "daily_summary": "summary-id",
        "wrongly_trashed": "feedback-id",
    }
    runner.config = SimpleNamespace(
        mode="active",
        min_trash_confidence=0.85,
        max_messages_per_run=50,
        recent_messages_per_run=20,
        candidate_scan_limit=5000,
        labels={
            "kept": "AI/Kept",
            "action_needed": "AI/Action-Needed",
            "digest_and_trash": "AI/Digest-and-Trash",
            "daily_summary": "AI/FOMO-Summarized",
            "wrongly_trashed": "AI/Wrongly-Trashed",
        },
        daily_summary=SimpleNamespace(
            enabled=daily_summary_enabled,
            decisions={"digest_and_trash"},
            trash_after_send=True,
            send_when_empty=True,
            subject_prefix="Today's GMAIL FOMO summary",
        ),
    )
    return runner


def test_collect_candidates_keeps_recent_messages_then_processes_older_backlog():
    runner = _runner()
    runner.gmail = _Gmail(candidate_ids=["newest", "newer", "middle", "older", "oldest"])
    runner.config.candidate_queries = ["in:inbox"]
    runner.config.max_messages_per_run = 4
    runner.config.recent_messages_per_run = 2
    runner.config.candidate_scan_limit = 5

    ids = runner._collect_candidates()

    assert ids == ["newest", "newer", "oldest", "older", "middle"]
    assert runner.gmail.calls == [("list", "in:inbox", 5)]


def test_existing_digest_label_is_queued_for_summary():
    runner = _runner(_context(), daily_summary_enabled=True)
    runner.gmail = _Gmail(_context(), candidate_ids=["m1"])

    items, message_ids, errors = runner._collect_pending_digest_items(set())

    assert errors == 0
    assert message_ids == {"m1"}
    assert len(items) == 1
    assert items[0].result.decision == "digest_and_trash"
    assert (
        "list",
        "in:anywhere label:AI/Digest-and-Trash -label:AI/FOMO-Summarized -label:AI/Wrongly-Trashed",
        50,
    ) in runner.gmail.calls
    assert runner.audit.records[-1].action_taken == "queued_existing_for_daily_summary"


def test_active_mode_trashes_confident_digest_without_summary():
    runner = _runner()
    result = ClassificationResult("digest_and_trash", 0.95, "bulk mail", "summary")

    action = runner._apply_decision(_context(), result)

    assert action == "trashed"
    assert runner.gmail.calls == [
        ("remove", "m1", "kept-id"),
        ("remove", "m1", "action-id"),
        ("add", "m1", "digest-id"),
        ("trash", "m1"),
    ]


def test_low_confidence_digest_is_deferred_without_label_or_trash():
    runner = _runner(daily_summary_enabled=True)
    result = ClassificationResult("digest_and_trash", 0.84, "uncertain bulk mail", "summary")

    action = runner._apply_decision(_context(), result)

    assert action == "deferred_low_confidence"
    assert runner._should_digest(result) is False
    assert runner.gmail.calls == []


def test_daily_summary_sends_then_trashes_digest_items():
    runner = _runner(daily_summary_enabled=True)
    context = _context()
    result = ClassificationResult("digest_and_trash", 0.99, "inbox noise", "summary")

    stats = runner._send_daily_summary([SimpleNamespace(context=context, result=result, bullets=["Key point"])])

    assert stats == {"summarized": 1, "summary_sent": 1, "trashed": 1}
    assert runner.gmail.calls[0] == ("profile",)
    assert runner.gmail.calls[1][0] == "send"
    assert runner.gmail.calls[1][1] == "me@example.com"
    assert "Key point" in runner.gmail.calls[1][3]
    assert runner.gmail.calls[2:] == [("add", "m1", "summary-id"), ("trash", "m1")]
    assert runner.audit.records[-1].action_taken == "summarized_and_trashed"


def test_starred_message_is_not_trashed_by_apply_decision():
    runner = _runner()
    result = ClassificationResult("digest_and_trash", 0.99, "bulk mail", "summary")

    action = runner._apply_decision(_context(labels=["INBOX", "STARRED"]), result)

    assert action == "protected_starred"
    assert runner.gmail.calls == [
        ("remove", "m1", "action-id"),
        ("remove", "m1", "digest-id"),
        ("add", "m1", "kept-id"),
    ]


def test_starred_summary_item_is_not_trashed():
    runner = _runner(daily_summary_enabled=True)
    context = _context(labels=["INBOX", "STARRED"])
    result = ClassificationResult("digest_and_trash", 0.99, "inbox noise", "summary")

    stats = runner._send_daily_summary([SimpleNamespace(context=context, result=result, bullets=["Key point"])])

    assert stats == {"summarized": 0, "summary_sent": 1, "trashed": 0}
    assert ("add", "m1", "kept-id") in runner.gmail.calls
    assert ("trash", "m1") not in runner.gmail.calls
    assert runner.audit.records[-1].action_taken == "protected_starred"


def test_wrongly_trashed_feedback_restores_and_protects_sender():
    runner = _runner(_context(labels=["TRASH"]))

    senders, message_ids, restored = runner._process_feedback()

    assert senders == {"sender@example.com"}
    assert message_ids == {"m1"}
    assert restored == 1
    assert ("untrash", "m1") in runner.gmail.calls
    assert ("add", "m1", "INBOX") in runner.gmail.calls
    assert ("remove", "m1", "digest-id") in runner.gmail.calls
    assert ("remove", "m1", "summary-id") in runner.gmail.calls
    assert ("add", "m1", "kept-id") in runner.gmail.calls
