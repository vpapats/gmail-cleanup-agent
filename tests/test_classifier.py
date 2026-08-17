import json

from src.classifier import _build_openrouter_prompt, _sender_is_approved, classify_message
from src.feedback import build_feedback_example
from src.models import AttachmentContext, ClassificationResult, MessageContext


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "decision": "kept",
                                "confidence": 0.91,
                                "reason": "Attachment appears to be a useful reference.",
                                "summary": "Reference document from the sender.",
                            }
                        )
                    }
                }
            ]
        }


def test_openrouter_model_can_sort_attachment_only_message(monkeypatch):
    calls = []

    def fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr("src.classifier.requests.post", fake_post)

    context = MessageContext(
        message_id="m1",
        thread_id="t1",
        sender="store@example.com",
        subject="Reference document",
        snippet="Document attached",
        body_text="Sharing this for your records.",
        has_attachments=True,
        is_reply_thread=False,
        attachments=[
            AttachmentContext(
                filename="reference.pdf",
                mime_type="application/pdf",
                size=100,
                data_url="data:application/pdf;base64,ZmFrZQ==",
            )
        ],
    )

    result = classify_message(context, approved_trash_senders=set(), use_model=True)

    assert result.decision == "kept"
    assert result.protection_hits == ["has_attachments"]
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-or-test"
    assert calls[0]["json"]["model"] == "google/gemini-3.1-flash-lite"
    user_content = calls[0]["json"]["messages"][1]["content"]
    assert "initial rule decision is a hint, not a restriction" in user_content[0]["text"].lower()
    assert user_content[1]["type"] == "file"
    assert user_content[1]["file"]["filename"] == "reference.pdf"


def test_openrouter_can_send_rule_kept_message_to_digest(monkeypatch):
    class TrashResponse(_Response):
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "digest_and_trash",
                                    "confidence": 0.99,
                                    "reason": "Model wanted to trash it.",
                                    "summary": "A normal message.",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr("src.classifier.requests.post", lambda *args, **kwargs: TrashResponse())

    context = MessageContext(
        message_id="m1",
        thread_id="t1",
        sender="Bloomberg <noreply@news.bloomberg.com>",
        subject="ASML boosts sales forecast",
        snippet="Bloomberg Morning Briefing Europe",
        body_text="A morning markets and technology briefing.",
        has_attachments=False,
        is_reply_thread=False,
    )

    result = classify_message(context, approved_trash_senders=set(), use_model=True)

    assert result.decision == "digest_and_trash"
    assert result.confidence == 0.99


def test_openrouter_cannot_send_starred_message_to_digest(monkeypatch):
    class TrashResponse(_Response):
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "digest_and_trash",
                                    "confidence": 0.99,
                                    "reason": "Bulk newsletter.",
                                    "summary": "Newsletter summary.",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr("src.classifier.requests.post", lambda *args, **kwargs: TrashResponse())

    context = MessageContext(
        message_id="m-starred",
        thread_id="t-starred",
        sender="News <brief@news.bloomberg.com>",
        subject="Prompt Engineering Is Dead. Good",
        snippet="newsletter unsubscribe promo discount",
        body_text="Low-priority-looking content",
        has_attachments=False,
        is_reply_thread=False,
        labels=["INBOX", "STARRED"],
    )

    result = classify_message(
        context,
        approved_trash_senders={"news.bloomberg.com"},
        use_model=True,
    )

    assert result.decision == "kept"
    assert "starred" in result.protection_hits


def test_feedback_does_not_protect_every_email_from_same_sender():
    corrected = MessageContext(
        message_id="corrected",
        thread_id="t-corrected",
        sender="Trusted Sender <trusted@example.com>",
        subject="Warranty for order #123",
        snippet="Your warranty document",
        body_text="Warranty certificate for order #123",
        has_attachments=True,
        is_reply_thread=False,
        attachments=[
            AttachmentContext(
                filename="warranty-order-123.pdf",
                mime_type="application/pdf",
                size=100,
            )
        ],
    )
    context = MessageContext(
        message_id="m2",
        thread_id="t2",
        sender="Trusted Sender <trusted@example.com>",
        subject="Newsletter",
        snippet="unsubscribe promo discount",
        body_text="Low-priority-looking content",
        has_attachments=False,
        is_reply_thread=False,
    )

    result = classify_message(
        context,
        approved_trash_senders={"example.com"},
        feedback_examples=[build_feedback_example(corrected)],
    )

    assert result.decision == "digest_and_trash"
    assert "user_feedback" not in result.protection_hits


def test_product_warranty_attachment_is_hard_kept_even_if_model_requests_trash(monkeypatch):
    class TrashResponse(_Response):
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "digest_and_trash",
                                    "confidence": 0.99,
                                    "reason": "Promotional sender.",
                                    "summary": "Promotion.",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr("src.classifier.requests.post", lambda *args, **kwargs: TrashResponse())
    context = MessageContext(
        message_id="warranty",
        thread_id="t-warranty",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Your purchase documents",
        snippet="Promotion and product information",
        body_text="Mostly promotional content.",
        has_attachments=True,
        is_reply_thread=False,
        attachments=[
            AttachmentContext(
                filename="warranty-certificate.pdf",
                mime_type="application/pdf",
                size=100,
            )
        ],
    )

    result = classify_message(
        context,
        approved_trash_senders={"plaisio.gr"},
        use_model=True,
    )

    assert result.decision == "kept"
    assert "product_warranty_record" in result.protection_hits


def test_extended_warranty_promotion_is_not_treated_as_existing_warranty_record():
    context = MessageContext(
        message_id="promo",
        thread_id="t-promo",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Discount on an extended warranty",
        snippet="Buy extended warranty today. Promo deal and discount.",
        body_text="Special offer for a new extended warranty.",
        has_attachments=False,
        is_reply_thread=False,
    )

    result = classify_message(
        context,
        approved_trash_senders={"plaisio.gr"},
    )

    assert result.decision == "digest_and_trash"
    assert "product_warranty_record" not in result.protection_hits


def test_classifier_prompt_uses_only_content_relevant_feedback_examples():
    corrected_context = MessageContext(
        message_id="corrected",
        thread_id="t-corrected",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Warranty for order #123",
        snippet="Warranty certificate",
        body_text="Warranty certificate for order #123",
        has_attachments=True,
        is_reply_thread=False,
        attachments=[
            AttachmentContext(
                filename="warranty-order-123.pdf",
                mime_type="application/pdf",
                size=100,
            )
        ],
    )
    example = build_feedback_example(corrected_context)
    initial = ClassificationResult("kept", 0.99, "caution", "summary")
    similar = MessageContext(
        message_id="similar",
        thread_id="t-similar",
        sender="Shop <orders@example-shop.gr>",
        subject="Your product documents",
        snippet="Documents attached",
        body_text="Purchase documents",
        has_attachments=True,
        is_reply_thread=False,
        attachments=[
            AttachmentContext(
                filename="product-warranty.pdf",
                mime_type="application/pdf",
                size=100,
            )
        ],
    )
    ordinary_promo = MessageContext(
        message_id="promo",
        thread_id="t-promo",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Weekend discount",
        snippet="Promo deal and discount",
        body_text="Buy now.",
        has_attachments=False,
        is_reply_thread=False,
    )

    similar_prompt = _build_openrouter_prompt(similar, initial, [example])
    promo_prompt = _build_openrouter_prompt(ordinary_promo, initial, [example])

    assert "Warranty for order #123" in similar_prompt
    assert "Warranty for order #123" not in promo_prompt


def test_starred_message_is_always_kept():
    context = MessageContext(
        message_id="m3",
        thread_id="t3",
        sender="News <brief@news.bloomberg.com>",
        subject="Prompt Engineering Is Dead. Good",
        snippet="newsletter unsubscribe promo discount",
        body_text="Low-priority-looking content",
        has_attachments=False,
        is_reply_thread=False,
        labels=["INBOX", "STARRED"],
    )

    result = classify_message(context, approved_trash_senders={"news.bloomberg.com"})

    assert result.decision == "kept"
    assert "starred" in result.protection_hits


def test_approved_sender_matching_uses_address_not_display_name():
    assert _sender_is_approved("News <brief@news.bloomberg.com>", {"news.bloomberg.com"})
    assert not _sender_is_approved(
        "news.bloomberg.com <attacker@example.net>",
        {"news.bloomberg.com"},
    )
