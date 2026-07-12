from datetime import date

import pytest

from src.weekly_auditor import (
    AuditCollection,
    DailyDecision,
    IndependentReview,
    WeekRange,
    WeeklyQualityAuditor,
    _build_independent_review_prompt,
    _parse_daily_decisions,
    build_weekly_email,
    validate_weekly_email,
)


def _decision(message_id="m1", label="kept", confidence=0.95):
    return DailyDecision(
        run_id=101,
        message_id=message_id,
        sender="Sender <sender@example.com>",
        subject=f"Subject {message_id}",
        label=label,
        confidence=confidence,
        reason="daily reason must not be sent to independent review",
    )


def _review(decision=None, expected="kept", certainty="clear", important=False):
    return IndependentReview(
        decision=decision or _decision(),
        expected_label=expected,
        certainty=certainty,
        evidence="Το περιεχόμενο υποστηρίζει διαφορετική προτεραιότητα.",
        important_attention=important,
    )


def test_previous_week_uses_complete_calendar_week():
    week = WeekRange.previous(date(2026, 7, 12))

    assert week.start == date(2026, 6, 29)
    assert week.end == date(2026, 7, 6)
    assert week.display == "2026-06-29 – 2026-07-05"


def test_parse_daily_decisions_deduplicates_operational_records():
    records = [
        {
            "message_id": "m1",
            "sender": "a@example.com",
            "subject": "One",
            "decision": "digest_and_trash",
            "confidence": 0.94,
            "reason": "noise",
            "action_taken": "queued_for_daily_summary",
        },
        {
            "message_id": "m1",
            "decision": "digest_and_trash",
            "confidence": 1,
            "action_taken": "summarized_and_trashed",
        },
    ]

    decisions, malformed = _parse_daily_decisions(1, records)

    assert malformed == 0
    assert len(decisions) == 1
    assert decisions[0].confidence == 0.94


def test_independent_prompt_hides_daily_label_reason_and_confidence():
    decision = _decision(label="digest_and_trash", confidence=0.99)
    context = type(
        "Context",
        (),
        {
            "sender": "sender@example.com",
            "subject": "Subject",
            "snippet": "Snippet",
            "body_text": "Body",
            "has_attachments": False,
            "attachments": [],
            "is_reply_thread": False,
            "labels": [],
        },
    )()

    prompt = _build_independent_review_prompt([decision], {decision.message_id: context})

    assert decision.reason not in prompt
    assert "0.99" not in prompt
    assert '"original_label"' not in prompt
    assert "101:m1" in prompt


def test_weekly_email_counts_are_consistent_and_under_200_words():
    reviews = [
        _review(_decision("m1", "kept"), "kept"),
        _review(_decision("m2", "digest_and_trash"), "action_needed", important=True),
        _review(_decision("m3", "kept"), None, certainty="ambiguous"),
    ]

    subject, body = build_weekly_email(
        WeekRange(date(2026, 6, 29), date(2026, 7, 6)), 7, reviews
    )

    assert subject == "Weekly Review — Attention Needed"
    assert "Ελέγχθηκαν 3 emails από 7 καθημερινά runs." in body
    assert "• 1 labels φαίνονται σωστά" in body
    assert "• 1 χρειάζονται επανέλεγχο" in body
    assert "• 1 περιπτώσεις ήταν ασαφείς" in body
    assert len(body.split()) < 200
    validate_weekly_email(subject, body, reviews)


def test_email_lists_no_more_than_three_flagged_messages():
    reviews = [
        _review(_decision(f"m{i}", "kept"), "action_needed") for i in range(5)
    ]

    _, body = build_weekly_email(
        WeekRange(date(2026, 6, 29), date(2026, 7, 6)), 7, reviews
    )

    assert body.count("Sender <sender@example.com> — Subject") == 3


def test_incomplete_data_is_disclosed_without_inventing_results():
    subject, body = build_weekly_email(
        WeekRange(date(2026, 6, 29), date(2026, 7, 6)),
        6,
        [],
        incomplete_notes=["Έλειπαν artifacts από 1 runs."],
    )

    assert subject == "Weekly Review — 2026-06-29 – 2026-07-05"
    assert "Απαιτείται έλεγχος" in body
    assert "Έλειπαν artifacts από 1 runs." in body


def test_ambiguous_results_are_not_reported_as_good_or_correct():
    reviews = [_review(_decision(), None, certainty="ambiguous")]

    _, body = build_weekly_email(
        WeekRange(date(2026, 6, 29), date(2026, 7, 6)), 7, reviews
    )

    assert "Συνολική εικόνα: Χρειάζεται προσοχή" in body
    assert "Δεν θεωρήθηκαν λανθασμένες χωρίς τεκμηρίωση." in body
    assert "Επανελέγξτε τις ασαφείς περιπτώσεις" in body


class _Gmail:
    def __init__(self, already_sent=False):
        self.sent = []
        self.already_sent = already_sent

    def message_exists_by_rfc822_message_id(self, message_id):
        return self.already_sent

    def get_profile_email(self):
        return "me@example.com"

    def get_message_context(self, message_id):
        raise RuntimeError("unavailable")

    def send_email(self, to_address, subject, body, *, message_id_header=None):
        self.sent.append((to_address, subject, body, message_id_header))
        return "sent"


class _Source:
    def collect(self, week):
        return AuditCollection(run_count=1, decisions=[_decision()])


def test_runner_sends_exactly_one_email_when_message_data_is_unavailable():
    gmail = _Gmail()
    auditor = WeeklyQualityAuditor(
        gmail=gmail,
        source=_Source(),
        api_key="test-key",
        model="test-model",
    )

    stats = auditor.run(WeekRange(date(2026, 6, 29), date(2026, 7, 6)))

    assert stats["email_sent"] == 1
    assert len(gmail.sent) == 1
    assert "Δεν ανακτήθηκε το περιεχόμενο 1 emails." in gmail.sent[0][2]


def test_runner_does_not_send_duplicate_weekly_email():
    gmail = _Gmail(already_sent=True)
    auditor = WeeklyQualityAuditor(
        gmail=gmail,
        source=_Source(),
        api_key="test-key",
        model="test-model",
    )

    stats = auditor.run(WeekRange(date(2026, 6, 29), date(2026, 7, 6)))

    assert stats == {"email_sent": 0, "already_sent": 1}
    assert gmail.sent == []


def test_validation_rejects_200_words_or_more():
    with pytest.raises(ValueError, match="under 200 words"):
        validate_weekly_email(
            "Weekly Review — 2026-06-29 – 2026-07-05",
            "Συνολική εικόνα: Καλή Κύριο συμπέρασμα Προσοχή Πρόταση "
            + "word " * 200
            + "Δεν πραγματοποιήθηκαν αλλαγές στα labels.",
            [],
        )
