from datetime import date

import json

from src.digest import DigestItem, build_daily_summary, summarize_for_digest
from src.feedback import FeedbackReview
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


def test_daily_summary_includes_wrongly_trashed_review_without_separate_email():
    context = MessageContext(
        message_id="feedback-1",
        thread_id="thread-feedback-1",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Warranty information",
        snippet="Warranty document",
        body_text="Warranty for order #123",
        has_attachments=True,
        is_reply_thread=False,
        received_at="2026-08-16T08:00:00+00:00",
    )
    review = FeedbackReview(
        context=context,
        reason="Περιέχει έγγραφο εγγύησης.",
        lesson="Διατήρηση εγγυήσεων ανά email, όχι ανά αποστολέα.",
        evidence=("warranty.pdf",),
        certainty="high",
    )

    body = build_daily_summary([], date(2026, 8, 17), [review])

    assert body.startswith("Today's GMAIL FOMO summary - 2026-08-17")
    assert "Corrections from AI/Wrongly-Trashed" in body
    assert "Warranty information" in body
    assert "Result: Restored to Inbox and labeled AI/Kept" in body
    assert "No digest-and-trash emails needed a summary today." in body


def test_digest_accepts_json_in_model_content_blocks(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(
                                        {"bullets": ["First fact.", "Second fact."]}
                                    ),
                                }
                            ]
                        }
                    }
                ]
            }

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        "src.digest.requests.post",
        lambda *args, **kwargs: Response(),
    )
    context = MessageContext(
        message_id="m-blocks",
        thread_id="t-blocks",
        sender="News <news@example.com>",
        subject="Daily news",
        snippet="Two useful facts.",
        body_text="Two useful facts.",
        has_attachments=False,
        is_reply_thread=False,
    )
    result = ClassificationResult(
        "digest_and_trash",
        0.99,
        "Newsletter",
        "Fallback summary.",
    )

    assert summarize_for_digest(context, result) == ["First fact.", "Second fact."]


def test_digest_falls_back_safely_for_non_object_model_content(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"content": [{"type": "text", "text": "[]"}]}}
                ]
            }

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(
        "src.digest.requests.post",
        lambda *args, **kwargs: Response(),
    )
    context = MessageContext(
        message_id="m-invalid",
        thread_id="t-invalid",
        sender="News <news@example.com>",
        subject="Daily news",
        snippet="A useful fact.",
        body_text="A useful fact.",
        has_attachments=False,
        is_reply_thread=False,
    )
    result = ClassificationResult(
        "digest_and_trash",
        0.99,
        "Newsletter",
        "Fallback summary.",
    )

    assert summarize_for_digest(context, result) == ["Fallback summary."]
