from datetime import date

from src.digest import DigestItem, build_daily_summary
from src.models import ClassificationResult, MessageContext


def test_daily_summary_shows_each_messages_gmail_receipt_date_in_athens():
    context = MessageContext(
        message_id="m1",
        thread_id="t1",
        sender="Sender <sender@example.com>",
        subject="Older newsletter",
        snippet="",
        body_text="",
        has_attachments=False,
        is_reply_thread=False,
        received_at="2026-07-31T22:30:00+00:00",
    )
    result = ClassificationResult(
        decision="digest_and_trash",
        confidence=0.99,
        reason="newsletter",
        summary="Older inbox item",
    )

    body = build_daily_summary(
        [DigestItem(context=context, result=result, bullets=["Older inbox item"])],
        date(2026, 8, 9),
    )

    assert "Received: 2026-08-01" in body


def test_daily_summary_discloses_when_receipt_date_is_unavailable():
    context = MessageContext(
        message_id="m1",
        thread_id="t1",
        sender="sender@example.com",
        subject="No date",
        snippet="",
        body_text="",
        has_attachments=False,
        is_reply_thread=False,
    )
    result = ClassificationResult("digest_and_trash", 0.99, "noise", "Noise")

    body = build_daily_summary(
        [DigestItem(context=context, result=result, bullets=["Noise"])],
        date(2026, 8, 9),
    )

    assert "Received: unknown" in body
