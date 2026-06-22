import json

from src.classifier import classify_message
from src.models import AttachmentContext, MessageContext


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
                                "decision": "keep",
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

    assert result.decision == "keep"
    assert result.protection_hits == []
    assert calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-or-test"
    assert calls[0]["json"]["model"] == "google/gemini-3.1-flash-lite"
    user_content = calls[0]["json"]["messages"][1]["content"]
    assert user_content[1]["type"] == "file"
    assert user_content[1]["file"]["filename"] == "reference.pdf"


def test_openrouter_cannot_upgrade_non_trash_message_to_trash(monkeypatch):
    class TrashResponse(_Response):
        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "decision": "summarize_then_trash",
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
        sender="person@example.com",
        subject="hello",
        snippet="Checking in",
        body_text="Can we talk tomorrow?",
        has_attachments=False,
        is_reply_thread=False,
    )

    result = classify_message(context, approved_trash_senders=set(), use_model=True)

    assert result.decision == "review"
