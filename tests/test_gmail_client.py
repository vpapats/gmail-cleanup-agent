from src.gmail_client import GmailClient


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
