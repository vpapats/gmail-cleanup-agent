import base64
import json
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from src.gmail_client import LEGACY_FEEDBACK_STATE_SUBJECT, GmailClient


class _Exec:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _Messages:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        page_token = kwargs.get("pageToken")
        return _Exec(self._responses.get(page_token))


class _Users:
    def __init__(self, messages):
        self._messages = messages

    def messages(self):
        return self._messages


class _Service:
    def __init__(self, messages):
        self._users = _Users(messages)

    def users(self):
        return self._users


def _client_with_responses(responses):
    client = GmailClient.__new__(GmailClient)
    messages = _Messages(responses)
    client.service = _Service(messages)
    client._with_retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    return client, messages


def test_list_candidates_paginates_until_max_messages():
    client, messages = _client_with_responses(
        {
            None: {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "p2"},
            "p2": {"messages": [{"id": "m3"}], "nextPageToken": "p3"},
            "p3": {"messages": [{"id": "m4"}]},
        }
    )

    ids = client.list_candidates("in:inbox", max_messages=3)

    assert ids == ["m1", "m2", "m3"]
    assert len(messages.calls) == 2


def test_list_candidates_stops_when_no_next_page():
    client, _ = _client_with_responses(
        {
            None: {"messages": [{"id": "m1"}, {"id": "m2"}]},
        }
    )

    ids = client.list_candidates("in:inbox", max_messages=10)

    assert ids == ["m1", "m2"]


def test_list_candidates_paginates_entire_history_when_limit_is_none():
    client, messages = _client_with_responses(
        {
            None: {"messages": [{"id": "new"}], "nextPageToken": "old-page"},
            "old-page": {"messages": [{"id": "oldest"}]},
        }
    )

    ids = client.list_candidates("in:inbox", max_messages=None)

    assert ids == ["new", "oldest"]
    assert [call["maxResults"] for call in messages.calls] == [500, 500]


def test_unlimited_candidate_scan_reaches_beyond_former_5000_id_cap():
    responses = {}
    for page in range(11):
        token = None if page == 0 else f"page-{page}"
        next_token = f"page-{page + 1}" if page < 10 else None
        response = {
            "messages": [
                {"id": f"message-{page}-{index}"}
                for index in range(500)
            ]
        }
        if next_token:
            response["nextPageToken"] = next_token
        responses[token] = response
    client, messages = _client_with_responses(responses)

    ids = client.list_candidates("in:inbox", max_messages=None)

    assert len(ids) == 5500
    assert ids[-1] == "message-10-499"
    assert len(messages.calls) == 11


def test_previous_year_received_date_scan_uses_athens_boundaries_and_skips_invalid_leap_dates():
    client = GmailClient.__new__(GmailClient)
    queries = []
    athens = ZoneInfo("Europe/Athens")
    leap_day = date(2020, 2, 29)
    start = datetime.combine(leap_day, time.min, tzinfo=athens).astimezone(timezone.utc)
    end = datetime.combine(
        leap_day + timedelta(days=1),
        time.min,
        tzinfo=athens,
    ).astimezone(timezone.utc)
    expected_query_window = (
        f"after:{int(start.timestamp()) - 1} before:{int(end.timestamp())}"
    )

    def list_candidates(query, max_messages):
        queries.append(query)
        if expected_query_window in query:
            return ["excluded", "m-leap"]
        return []

    client.list_candidates = list_candidates
    client.get_message_metadata = lambda message_id: {
        "message_id": message_id,
        "sender": "sender@example.com",
        "subject": "Leap-day email",
        "received_at": "2020-02-28T22:15:00+00:00",
    }

    matches = client.list_previous_year_received_date_matches(
        date(2024, 2, 29),
        excluded_message_ids={"excluded"},
    )

    assert len(queries) == 13
    assert all("-in:trash" in query and "-in:spam" in query for query in queries)
    assert matches[0].year == 2020
    assert matches[0].matched_count == 1
    assert matches[0].received_at == "2020-02-28T22:15:00+00:00"


def test_previous_year_received_date_scan_preserves_truncated_count_after_exclusions():
    client = GmailClient.__new__(GmailClient)
    candidate_ids = ["excluded", *(f"m-{index}" for index in range(499))]
    first_query = True

    def list_candidates(query, max_messages):
        nonlocal first_query
        if first_query:
            first_query = False
            assert max_messages == 500
            return candidate_ids
        return []

    client.list_candidates = list_candidates
    client.get_message_metadata = lambda message_id: {
        "message_id": message_id,
        "sender": "sender@example.com",
        "subject": "Annual notice",
        "received_at": "2025-08-29T08:00:00+00:00",
    }

    matches = client.list_previous_year_received_date_matches(
        date(2026, 8, 29),
        excluded_message_ids={"excluded"},
    )

    assert matches[0].year == 2025
    assert matches[0].matched_count == 499
    assert matches[0].count_is_lower_bound is True


def test_weekly_message_id_lookup_uses_gmail_rfc822_search_without_brackets():
    client, messages = _client_with_responses({None: {"messages": [{"id": "sent-1"}]}})

    exists = client.message_exists_by_rfc822_message_id("<weekly@gmail-fomo.local>")

    assert exists is True
    assert messages.calls == [
        {
            "userId": "me",
            "q": "in:anywhere rfc822msgid:weekly@gmail-fomo.local",
            "maxResults": 1,
        }
    ]


class _MessagesWithGet(_Messages):
    def get(self, **kwargs):
        assert kwargs == {"userId": "me", "id": "m1", "format": "full"}
        return _Exec(
            {
                "id": "m1",
                "threadId": "t1",
                "internalDate": "1785537000000",
                "payload": {"headers": []},
            }
        )


def test_message_context_uses_gmail_internal_receipt_timestamp():
    client = GmailClient.__new__(GmailClient)
    client.service = _Service(_MessagesWithGet({}))
    client._with_retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    client._extract_body = lambda payload: ""
    client._has_attachments = lambda payload: False
    client._extract_attachments = lambda message_id, payload: []

    context = client.get_message_context("m1")

    assert context.received_at == "2026-07-31T22:30:00+00:00"


def _feedback_state_message(state):
    data = base64.urlsafe_b64encode(json.dumps(state).encode("utf-8")).decode("ascii")
    return {
        "payload": {
            "headers": [{"name": "Subject", "value": LEGACY_FEEDBACK_STATE_SUBJECT}],
            "body": {"data": data},
        }
    }


def test_feedback_state_parser_deduplicates_message_ids():
    client = GmailClient.__new__(GmailClient)

    message_ids = client._parse_legacy_feedback_state_message(
        _feedback_state_message({"version": 1, "message_ids": ["m1", "m2", "m1"]})
    )

    assert message_ids == ["m1", "m2"]


def test_feedback_state_parser_rejects_unsupported_version():
    client = GmailClient.__new__(GmailClient)

    with pytest.raises(RuntimeError, match="unsupported version"):
        client._parse_legacy_feedback_state_message(
            _feedback_state_message({"version": 2, "message_ids": ["m1"]})
        )


class _AttachmentGet:
    def __init__(self, response):
        self._response = response

    def execute(self):
        return self._response


class _Attachments:
    def get(self, **kwargs):
        assert kwargs["id"] == "a1"
        return _AttachmentGet({"data": "aGVsbG8td29ybGQ"})


class _MessagesWithAttachments(_Messages):
    def attachments(self):
        return _Attachments()


class _UsersWithAttachments(_Users):
    def __init__(self):
        self._messages = _MessagesWithAttachments({})


class _ServiceWithAttachments:
    def users(self):
        return _UsersWithAttachments()


def test_extract_attachments_downloads_small_text_parts(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MAX_ATTACHMENT_BYTES", "100")
    client = GmailClient.__new__(GmailClient)
    client.service = _ServiceWithAttachments()
    client._with_retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    attachments = client._extract_attachments(
        "m1",
        {
            "parts": [
                {
                    "filename": "note.txt",
                    "mimeType": "text/plain",
                    "body": {"attachmentId": "a1", "size": 11},
                }
            ]
        },
    )

    assert len(attachments) == 1
    assert attachments[0].filename == "note.txt"
    assert attachments[0].text_sample == "hello-world"
