from __future__ import annotations

import base64
import time
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.auth import build_credentials
from src.models import MessageContext

USER_ID = "me"


class GmailClient:
    def __init__(self) -> None:
        creds = build_credentials()
        self.service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    def _with_retry(self, fn, *args, **kwargs):
        delay = 1.0
        for attempt in range(5):
            try:
                return fn(*args, **kwargs)
            except HttpError as err:
                status = getattr(err.resp, "status", None)
                if status in {429, 500, 503} and attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    def ensure_label(self, label_name: str) -> str:
        response = self._with_retry(self.service.users().labels().list(userId=USER_ID).execute)
        for label in response.get("labels", []):
            if label.get("name") == label_name:
                return label["id"]

        payload = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = self._with_retry(
            self.service.users().labels().create(userId=USER_ID, body=payload).execute
        )
        return created["id"]

    def list_candidates(self, query: str, max_messages: int = 1000) -> list[str]:
        collected: list[str] = []
        page_token: str | None = None

        while len(collected) < max_messages:
            page_size = min(500, max_messages - len(collected))
            response = self._with_retry(
                self.service.users()
                .messages()
                .list(
                    userId=USER_ID,
                    q=query,
                    maxResults=page_size,
                    pageToken=page_token,
                )
                .execute
            )
            collected.extend([m["id"] for m in response.get("messages", [])])
            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return collected

    def get_message_context(self, message_id: str) -> MessageContext:
        message = self._with_retry(
            self.service.users().messages().get(userId=USER_ID, id=message_id, format="full").execute
        )
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in message.get("payload", {}).get("headers", [])
        }
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        snippet = message.get("snippet", "")
        payload = message.get("payload", {})
        body_text = self._extract_body(payload)
        has_attachments = self._has_attachments(payload)
        is_reply_thread = bool(headers.get("in-reply-to") or headers.get("references"))
        return MessageContext(
            message_id=message_id,
            thread_id=message.get("threadId", ""),
            sender=sender,
            subject=subject,
            snippet=snippet,
            body_text=body_text,
            has_attachments=has_attachments,
            is_reply_thread=is_reply_thread,
            labels=message.get("labelIds", []),
        )

    def add_label(self, message_id: str, label_id: str) -> None:
        self._with_retry(
            self.service.users().messages().modify(
                userId=USER_ID,
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute
        )

    def trash_message(self, message_id: str) -> None:
        self._with_retry(self.service.users().messages().trash(userId=USER_ID, id=message_id).execute)

    def _extract_body(self, payload: dict[str, Any]) -> str:
        if not payload:
            return ""
        body = payload.get("body", {})
        data = body.get("data")
        if data:
            try:
                return base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")[:4000]
            except Exception:
                return ""
        for part in payload.get("parts", []) or []:
            text = self._extract_body(part)
            if text:
                return text
        return ""

    def _has_attachments(self, payload: dict[str, Any]) -> bool:
        if payload.get("filename"):
            return True
        body = payload.get("body", {})
        if body.get("attachmentId"):
            return True
        return any(self._has_attachments(p) for p in payload.get("parts", []) or [])
