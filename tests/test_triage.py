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
        "important": "important-id",
        "action_needed": "action-id",
        "low_priority": "low-id",
        "review": "review-id",
        "daily_summary": "summary-id",
        "wrongly_trashed": "feedback-id",
    }
    runner.config = SimpleNamespace(
        mode="active",
        min_trash_confidence=0.93,
        max_messages_per_run=50,
        candidate_scan_limit=5000,
        labels={"wrongly_trashed": "AI/Wrongly-Trashed"},
        daily_summary=SimpleNamespace(
            enabled=daily_summary_enabled,
            decisions={"review", "low_priority"},
            trash_after_send=True,
            send_when_empty=True,
            subject_prefix="Today's GMAIL FOMO summary",
        ),
    )
    return runner


def test_collect_candidates_scans_more_than_daily_review_limit_and_processes_older_first():
    runner = _runner()
    runner.gmail = _Gmail(candidate_ids=["newest", "middle", "oldest"])
    runner.config.candidate_queries = ["in:inbox"]
    runner.config.max_messages_per_run = 2
    runner.config.candidate_scan_limit = 3

    ids = runner._collect_candidates()

    assert ids == ["oldest", "middle", "newest"]
    assert runner.gmail.calls == [("list", "in:inbox", 3)]


def test_active_mode_trashes_only_confident_low_priority():
    runner = _runner()
    result = ClassificationResult("low_priority", 0.95, "bulk mail", "summary")

    action = runner._apply_decision(_context(), result)

    assert action == "trashed"
    assert runner.gmail.calls == [("add", "m1", "low-id"), ("trash", "m1")]


def test_active_mode_does_not_trash_review():
    runner = _runner()
    result = ClassificationResult("review", 0.99, "uncertain", "summary")

    action = runner._apply_decision(_context(), result)

    assert action == "labeled_review"
    assert runner.gmail.calls == [("add", "m1", "review-id")]


def test_daily_summary_sends_then_trashes_review_items():
    runner = _runner(daily_summary_enabled=True)
    context = _context()
    result = ClassificationResult("review", 0.99, "uncertain", "summary")

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
    result = ClassificationResult("low_priority", 0.99, "bulk mail", "summary")

    action = runner._apply_decision(_context(labels=["INBOX", "STARRED"]), result)

    assert action == "protected_starred"
    assert runner.gmail.calls == [("add", "m1", "important-id")]


def test_starred_summary_item_is_not_trashed():
    runner = _runner(daily_summary_enabled=True)
    context = _context(labels=["INBOX", "STARRED"])
    result = ClassificationResult("review", 0.99, "uncertain", "summary")

    stats = runner._send_daily_summary([SimpleNamespace(context=context, result=result, bullets=["Key point"])])

    assert stats == {"summarized": 0, "summary_sent": 1, "trashed": 0}
    assert ("add", "m1", "important-id") in runner.gmail.calls
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
    assert ("remove", "m1", "low-id") in runner.gmail.calls
    assert ("remove", "m1", "review-id") in runner.gmail.calls
    assert ("remove", "m1", "summary-id") in runner.gmail.calls
    assert ("add", "m1", "important-id") in runner.gmail.calls
