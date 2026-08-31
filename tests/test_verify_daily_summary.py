from __future__ import annotations

import base64
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.verify_daily_summary import (
    SummaryVerification,
    SummaryVerificationError,
    verify_daily_summary,
)


ATHENS = ZoneInfo("Europe/Athens")
TARGET_DATE = date(2026, 8, 29)
SUBJECT = "Today's GMAIL FOMO summary - 2026-08-29"


class Execute:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)

    def execute(self):
        if not self.outcomes:
            raise AssertionError("execute called too many times")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeMessages:
    def __init__(
        self,
        messages: list[dict],
        *,
        pages: dict[str | None, dict] | None = None,
        first_list_outcomes: list[object] | None = None,
    ):
        self.messages_by_id = {message["id"]: message for message in messages}
        self.pages = pages
        self.first_list_outcomes = first_list_outcomes
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.first_list_outcomes is not None:
            outcomes = self.first_list_outcomes
            self.first_list_outcomes = None
            return Execute(*outcomes)
        if self.pages is not None:
            return Execute(self.pages[kwargs.get("pageToken")])
        return Execute({"messages": [{"id": key} for key in self.messages_by_id]})

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        return Execute(self.messages_by_id[kwargs["id"]])


class FakeUsers:
    def __init__(self, messages: FakeMessages):
        self._messages = messages

    def messages(self):
        return self._messages


class FakeService:
    def __init__(
        self,
        messages: list[dict],
        *,
        pages: dict[str | None, dict] | None = None,
        first_list_outcomes: list[object] | None = None,
    ):
        self.messages_api = FakeMessages(
            messages,
            pages=pages,
            first_list_outcomes=first_list_outcomes,
        )
        self._users = FakeUsers(self.messages_api)

    def users(self):
        return self._users


def _encoded(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _message(
    message_id: str = "summary-1",
    *,
    subject: str = SUBJECT,
    body: str,
    labels: list[str] | None = None,
    sent_at: datetime | None = None,
    multipart: bool = False,
) -> dict:
    sent_at = sent_at or datetime(2026, 8, 29, 10, 30, tzinfo=ATHENS)
    body_payload = {"body": {"data": _encoded(body)}}
    payload = {
        "headers": [{"name": "Subject", "value": subject}],
        **(
            {"body": {}, "parts": [{"mimeType": "text/plain", **body_payload}]}
            if multipart
            else body_payload
        ),
    }
    return {
        "id": message_id,
        "labelIds": ["SENT"] if labels is None else labels,
        "internalDate": str(int(sent_at.timestamp() * 1000)),
        "payload": payload,
    }


def test_accepts_received_dates_from_full_gmail_history_without_age_cutoff():
    body = """Today's GMAIL FOMO summary

1. The oldest retained email
From: first@example.com
Received: 2004-04-01

2. A much older cleanup item
From: old@example.com
Received: 2021-01-15

3. A current item
From: now@example.com
Received: 2026-08-29
"""
    service = FakeService([_message(body=body)])

    result = verify_daily_summary(service, TARGET_DATE)

    assert result == SummaryVerification(
        message_id="summary-1",
        referenced_emails=3,
        received_dates=3,
    )
    list_call = service.messages_api.list_calls[0]
    assert list_call == {
        "userId": "me",
        "q": f'in:anywhere label:sent subject:"{SUBJECT}"',
        "maxResults": 100,
    }
    # The verifier's Gmail query deliberately has no after:/newer_than: cutoff.
    assert "after:" not in list_call["q"]
    assert "newer_than:" not in list_call["q"]


def test_accepts_multipart_body_and_filters_nonexact_subject_candidates():
    valid = _message(
        body="1. Old email\nReceived: 2010-06-03\n",
        multipart=True,
    )
    unrelated = _message(
        "other",
        subject=f"Re: {SUBJECT}",
        body="1. Other\nReceived: 2026-08-29\n",
    )

    result = verify_daily_summary(FakeService([unrelated, valid]), TARGET_DATE)

    assert result.message_id == "summary-1"
    assert result.received_dates == 1


def test_accepts_sent_summary_that_was_later_moved_to_trash():
    service = FakeService(
        [
            _message(
                body="1. Message\nReceived: 2026-08-29\n",
                labels=["SENT", "TRASH"],
            )
        ]
    )

    result = verify_daily_summary(service, TARGET_DATE)

    assert result.message_id == "summary-1"
    assert service.messages_api.list_calls[0]["q"] == (
        f'in:anywhere label:sent subject:"{SUBJECT}"'
    )


def test_requires_exactly_one_exact_subject_summary():
    body = "1. Message\nReceived: 2026-08-29\n"
    service = FakeService([_message("one", body=body), _message("two", body=body)])

    with pytest.raises(SummaryVerificationError, match="exactly one"):
        verify_daily_summary(service, TARGET_DATE)


def test_search_paginates_all_sent_results_and_deduplicates_message_ids():
    valid = _message(body="1. Old email\nReceived: 2005-02-01\n")
    unrelated = _message(
        "other",
        subject=f"Re: {SUBJECT}",
        body="1. Other\nReceived: 2026-08-29\n",
    )
    service = FakeService(
        [valid, unrelated],
        pages={
            None: {"messages": [{"id": "other"}], "nextPageToken": "page-2"},
            "page-2": {
                "messages": [{"id": "summary-1"}, {"id": "summary-1"}]
            },
        },
    )

    result = verify_daily_summary(service, TARGET_DATE, sleep=lambda _seconds: None)

    assert result.message_id == "summary-1"
    assert service.messages_api.list_calls == [
        {
            "userId": "me",
            "q": f'in:anywhere label:sent subject:"{SUBJECT}"',
            "maxResults": 100,
        },
        {
            "userId": "me",
            "q": f'in:anywhere label:sent subject:"{SUBJECT}"',
            "maxResults": 100,
            "pageToken": "page-2",
        },
    ]
    assert [call["id"] for call in service.messages_api.get_calls] == [
        "other",
        "summary-1",
    ]


def test_transient_gmail_search_failure_is_retried_with_backoff():
    class TransientError(Exception):
        def __init__(self):
            self.resp = type("Response", (), {"status": 503})()

    message = _message(body="1. Old email\nReceived: 2004-04-01\n")
    service = FakeService(
        [message],
        first_list_outcomes=[
            TransientError(),
            {"messages": [{"id": "summary-1"}]},
        ],
    )
    sleeps: list[float] = []

    result = verify_daily_summary(service, TARGET_DATE, sleep=sleeps.append)

    assert result.message_id == "summary-1"
    assert sleeps == [1.0]
    assert len(service.messages_api.list_calls) == 1


def test_requires_sent_label():
    service = FakeService(
        [_message(body="1. Message\nReceived: 2026-08-29\n", labels=["INBOX"])]
    )

    with pytest.raises(SummaryVerificationError, match="not labeled SENT"):
        verify_daily_summary(service, TARGET_DATE)


def test_requires_summary_internal_date_on_target_athens_day():
    service = FakeService(
        [
            _message(
                body="1. Message\nReceived: 2026-08-29\n",
                sent_at=datetime(2026, 8, 28, 23, 59, tzinfo=ATHENS),
            )
        ]
    )

    with pytest.raises(SummaryVerificationError, match="target Athens date"):
        verify_daily_summary(service, TARGET_DATE)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "1. One\nReceived: 2026-08-29\n\n2. Two has no date\nFrom: x@example.com\n",
            "entry 2 has 0 receipt dates; expected 1",
        ),
        (
            "1. One\nReceived: unknown\n",
            "entry 1 has an invalid receipt date",
        ),
        (
            "1. One\nReceived: 29-08-2026\n",
            "entry 1 has an invalid receipt date",
        ),
        (
            "1. One\nReceived: 2026-99-99\n",
            "entry 1 has an invalid receipt date",
        ),
    ],
)
def test_every_referenced_email_requires_one_iso_received_line(body, message):
    with pytest.raises(SummaryVerificationError, match=message):
        verify_daily_summary(FakeService([_message(body=body)]), TARGET_DATE)


def test_received_lines_cannot_be_reallocated_between_entries():
    body = """1. One
Received: 2026-08-28
Received: 2026-08-29

2. Two
From: x@example.com
"""

    with pytest.raises(
        SummaryVerificationError,
        match="entry 1 has 2 receipt dates; expected 1",
    ):
        verify_daily_summary(FakeService([_message(body=body)]), TARGET_DATE)


def test_valid_explicit_no_email_summary_is_accepted():
    result = verify_daily_summary(
        FakeService(
            [_message(body="No digest-and-trash emails needed a summary today.\n")]
        ),
        TARGET_DATE,
    )

    assert result.referenced_emails == 0
    assert result.received_dates == 0


def test_empty_or_unverifiable_no_entry_summary_is_rejected():
    with pytest.raises(SummaryVerificationError, match="no verifiable email entries"):
        verify_daily_summary(
            FakeService([_message(body="Nothing to report.\n")]),
            TARGET_DATE,
        )
