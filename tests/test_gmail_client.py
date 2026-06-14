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
