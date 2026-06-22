from types import SimpleNamespace

from src.models import ClassificationResult, MessageContext
from src.triage import TriageRunner


class _Gmail:
    def __init__(self, context=None):
        self.context = context
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
        return ["m1"]

    def get_message_context(self, message_id):
        return self.context


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


def _runner(context=None):
    runner = TriageRunner.__new__(TriageRunner)
    runner.gmail = _Gmail(context)
    runner.audit = _Audit()
    runner.label_ids = {
        "important": "important-id",
        "action_needed": "action-id",
        "low_priority": "low-id",
        "review": "review-id",
        "wrongly_trashed": "feedback-id",
    }
    runner.config = SimpleNamespace(
        mode="active",
        min_trash_confidence=0.93,
        labels={"wrongly_trashed": "AI/Wrongly-Trashed"},
    )
    return runner


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


def test_wrongly_trashed_feedback_restores_and_protects_sender():
    runner = _runner(_context(labels=["TRASH"]))

    senders, message_ids, restored = runner._process_feedback()

    assert senders == {"sender@example.com"}
    assert message_ids == {"m1"}
    assert restored == 1
    assert ("untrash", "m1") in runner.gmail.calls
    assert ("add", "m1", "INBOX") in runner.gmail.calls
    assert ("remove", "m1", "low-id") in runner.gmail.calls
    assert ("add", "m1", "important-id") in runner.gmail.calls
