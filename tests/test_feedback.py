from __future__ import annotations

from src.feedback import (
    build_feedback_example,
    has_product_warranty_record,
    review_wrongly_trashed,
    select_relevant_feedback_examples,
)
from src.models import AttachmentContext, MessageContext


def _message(
    *,
    message_id: str,
    sender: str,
    subject: str,
    snippet: str = "",
    body: str = "",
    attachment: str | None = None,
) -> MessageContext:
    attachments = []
    if attachment:
        attachments.append(
            AttachmentContext(
                filename=attachment,
                mime_type="application/pdf",
                size=100,
            )
        )
    return MessageContext(
        message_id=message_id,
        thread_id=f"thread-{message_id}",
        sender=sender,
        subject=subject,
        snippet=snippet,
        body_text=body,
        has_attachments=bool(attachments),
        is_reply_thread=False,
        attachments=attachments,
    )


def test_previous_correction_requires_content_similarity_not_same_domain():
    corrected = build_feedback_example(
        _message(
            message_id="corrected",
            sender="Plaisio <offers@plaisio.gr>",
            subject="Warranty for order #123",
            body="Warranty certificate for order #123",
            attachment="warranty-order-123.pdf",
        )
    )
    ordinary_promo = _message(
        message_id="promo",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Weekend discount",
        snippet="Promo deal and discount",
        body="Buy now and unsubscribe at any time.",
    )

    assert select_relevant_feedback_examples(ordinary_promo, [corrected]) == []


def test_warranty_signals_retrieve_prior_correction_even_from_another_sender():
    corrected = build_feedback_example(
        _message(
            message_id="corrected",
            sender="Plaisio <orders@plaisio.gr>",
            subject="Warranty for order #123",
            body="Warranty certificate for order #123",
            attachment="warranty-order-123.pdf",
        )
    )
    new_warranty = _message(
        message_id="new",
        sender="Another shop <orders@example-shop.gr>",
        subject="Your product documents",
        body="Documents for your purchase",
        attachment="product-warranty.pdf",
    )

    assert select_relevant_feedback_examples(new_warranty, [corrected]) == [corrected]


def test_warranty_feedback_review_ignores_expiration_and_avoids_sender_protection():
    context = _message(
        message_id="plaisio-warranty",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Product warranty",
        body="The warranty for order #123 expired last year.",
        attachment="warranty-certificate.pdf",
    )

    review = review_wrongly_trashed(context, [], use_model=False)

    assert review.certainty == "high"
    assert "λήξη" in review.reason
    assert "καθολική προστασία" in review.lesson
    assert any("warranty-certificate.pdf" in item for item in review.evidence)


def test_extended_warranty_offer_attachment_is_not_a_warranty_record():
    context = _message(
        message_id="warranty-offer",
        sender="Plaisio <offers@plaisio.gr>",
        subject="Extended warranty offer",
        snippet="Buy an extended warranty with a discount.",
        body="Promotional warranty offer.",
        attachment="extended-warranty-offer.pdf",
    )

    assert has_product_warranty_record(context) is False
