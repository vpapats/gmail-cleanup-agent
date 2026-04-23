from src.classifier import classify_message
from src.models import MessageContext


def ctx(**overrides):
    base = dict(
        message_id="m1",
        thread_id="t1",
        sender="Newsletter <noreply@news.example.com>",
        subject="Weekly newsletter and discount",
        snippet="unsubscribe promo deal",
        body_text="",
        has_attachments=False,
        is_reply_thread=False,
        labels=[],
    )
    base.update(overrides)
    return MessageContext(**base)


def test_protection_forces_review():
    result = classify_message(ctx(has_attachments=True), {"noreply@news.example.com"})
    assert result.decision == "review"
    assert "has_attachments" in result.protection_hits


def test_approved_sender_can_trash():
    result = classify_message(ctx(), {"noreply@news.example.com"})
    assert result.decision == "summarize_then_trash"


def test_nonapproved_sender_stays_review_or_keep():
    result = classify_message(ctx(sender="noreply@other.com"), {"noreply@news.example.com"})
    assert result.decision in {"review", "keep"}
