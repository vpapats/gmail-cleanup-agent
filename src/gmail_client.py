from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.auth import build_credentials
from src.models import AttachmentContext, MessageContext

USER_ID = "me"
FEEDBACK_STATE_SUBJECT = "GMAIL FOMO correction memory (do not send)"
FEEDBACK_STATE_VERSION = 1
DEFAULT_MAX_ATTACHMENT_BYTES = 750_000
TEXT_ATTACHMENT_MIME_PREFIXES = ("text/",)
TEXT_ATTACHMENT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "text/csv",
}
MODEL_FILE_MIME_TYPES = {"application/pdf"}
MODEL_IMAGE_MIME_PREFIXES = ("image/",)


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

    def get_profile_email(self) -> str:
        profile = self._with_retry(self.service.users().getProfile(userId=USER_ID).execute)
        return profile.get("emailAddress", "")

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
        attachments = self._extract_attachments(message_id, payload)
        is_reply_thread = bool(headers.get("in-reply-to") or headers.get("references"))
        received_at = ""
        try:
            received_at = datetime.fromtimestamp(
                int(message.get("internalDate", "")) / 1000,
                tz=timezone.utc,
            ).isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
        return MessageContext(
            message_id=message_id,
            thread_id=message.get("threadId", ""),
            sender=sender,
            subject=subject,
            snippet=snippet,
            body_text=body_text,
            has_attachments=has_attachments,
            is_reply_thread=is_reply_thread,
            received_at=received_at,
            labels=message.get("labelIds", []),
            attachments=attachments,
        )

    def add_label(self, message_id: str, label_id: str) -> None:
        self._with_retry(
            self.service.users().messages().modify(
                userId=USER_ID,
                id=message_id,
                body={"addLabelIds": [label_id]},
            ).execute
        )

    def remove_label(self, message_id: str, label_id: str) -> None:
        self._with_retry(
            self.service.users().messages().modify(
                userId=USER_ID,
                id=message_id,
                body={"removeLabelIds": [label_id]},
            ).execute
        )

    def message_exists_by_rfc822_message_id(self, message_id_header: str) -> bool:
        search_id = message_id_header.strip().strip("<>")
        response = self._with_retry(
            self.service.users()
            .messages()
            .list(
                userId=USER_ID,
                q=f"in:anywhere rfc822msgid:{search_id}",
                maxResults=1,
            )
            .execute
        )
        return bool(response.get("messages"))

    def send_email(
        self,
        to_address: str,
        subject: str,
        body_text: str,
        *,
        message_id_header: str | None = None,
    ) -> str:
        message = EmailMessage()
        message["To"] = to_address
        message["From"] = to_address
        message["Subject"] = subject
        if message_id_header:
            message["Message-ID"] = message_id_header
        message.set_content(body_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        response = self._with_retry(
            self.service.users().messages().send(
                userId=USER_ID,
                body={"raw": raw},
            ).execute
        )
        return response.get("id", "")

    def load_feedback_message_ids(self) -> list[str]:
        message_id = self._feedback_state_message_id()
        if not message_id:
            return []
        message = self._with_retry(
            self.service.users()
            .messages()
            .get(userId=USER_ID, id=message_id, format="full")
            .execute
        )
        return self._parse_feedback_state_message(message)

    def _parse_feedback_state_message(self, message: dict[str, Any]) -> list[str]:
        payload = message.get("payload", {})
        headers = {
            item.get("name", "").lower(): item.get("value", "")
            for item in payload.get("headers", [])
        }
        if headers.get("subject") != FEEDBACK_STATE_SUBJECT:
            return []
        try:
            state = json.loads(self._extract_body(payload, max_chars=100_000))
        except (TypeError, ValueError, json.JSONDecodeError) as err:
            raise RuntimeError("GMAIL FOMO correction memory draft is invalid") from err
        if not isinstance(state, dict) or state.get("version") != FEEDBACK_STATE_VERSION:
            raise RuntimeError("GMAIL FOMO correction memory draft has an unsupported version")
        message_ids = state.get("message_ids")
        if not isinstance(message_ids, list) or not all(
            isinstance(item, str) and item.strip() for item in message_ids
        ):
            raise RuntimeError("GMAIL FOMO correction memory draft has invalid message IDs")
        return list(dict.fromkeys(item.strip() for item in message_ids))

    def save_feedback_message_ids(self, message_ids: list[str]) -> None:
        normalized_ids = list(
            dict.fromkeys(
                item.strip()
                for item in message_ids
                if isinstance(item, str) and item.strip()
            )
        )
        message = EmailMessage()
        message["Subject"] = FEEDBACK_STATE_SUBJECT
        message.set_content(
            json.dumps(
                {"version": FEEDBACK_STATE_VERSION, "message_ids": normalized_ids},
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        draft_id = self._feedback_state_draft_id()
        if draft_id:
            saved = self._with_retry(
                self.service.users()
                .drafts()
                .update(
                    userId=USER_ID,
                    id=draft_id,
                    body={"message": {"raw": raw}},
                )
                .execute
            )
        else:
            saved = self._with_retry(
                self.service.users()
                .drafts()
                .create(userId=USER_ID, body={"message": {"raw": raw}})
                .execute
            )
        saved_draft_id = saved.get("id", "")
        if not saved_draft_id:
            raise RuntimeError("GMAIL FOMO correction memory draft returned no draft ID")
        verified = self._with_retry(
            self.service.users()
            .drafts()
            .get(userId=USER_ID, id=saved_draft_id, format="full")
            .execute
        )
        if self._parse_feedback_state_message(verified.get("message", {})) != normalized_ids:
            raise RuntimeError("GMAIL FOMO correction memory draft failed verification")

    def trash_message(self, message_id: str) -> None:
        self._with_retry(self.service.users().messages().trash(userId=USER_ID, id=message_id).execute)

    def untrash_message(self, message_id: str) -> None:
        self._with_retry(self.service.users().messages().untrash(userId=USER_ID, id=message_id).execute)

    def _feedback_state_message_id(self) -> str:
        response = self._with_retry(
            self.service.users()
            .messages()
            .list(
                userId=USER_ID,
                q=f'in:drafts subject:"{FEEDBACK_STATE_SUBJECT}"',
                maxResults=10,
            )
            .execute
        )
        for candidate in response.get("messages", []):
            message_id = candidate.get("id", "")
            if message_id:
                return message_id
        return ""

    def _feedback_state_draft_id(self) -> str:
        message_id = self._feedback_state_message_id()
        if not message_id:
            return ""
        page_token: str | None = None
        while True:
            response = self._with_retry(
                self.service.users()
                .drafts()
                .list(userId=USER_ID, maxResults=500, pageToken=page_token)
                .execute
            )
            for draft in response.get("drafts", []):
                if draft.get("message", {}).get("id") == message_id:
                    return draft.get("id", "")
            page_token = response.get("nextPageToken")
            if not page_token:
                return ""

    def _extract_body(self, payload: dict[str, Any], *, max_chars: int = 4000) -> str:
        if not payload:
            return ""
        body = payload.get("body", {})
        data = body.get("data")
        if data:
            try:
                padding = "=" * (-len(data) % 4)
                return base64.urlsafe_b64decode(data + padding).decode(
                    "utf-8", errors="ignore"
                )[:max_chars]
            except Exception:
                return ""
        for part in payload.get("parts", []) or []:
            text = self._extract_body(part, max_chars=max_chars)
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

    def _extract_attachments(self, message_id: str, payload: dict[str, Any]) -> list[AttachmentContext]:
        max_bytes = int(os.getenv("OPENROUTER_MAX_ATTACHMENT_BYTES", str(DEFAULT_MAX_ATTACHMENT_BYTES)))
        attachments: list[AttachmentContext] = []
        for part in self._walk_parts(payload):
            filename = part.get("filename") or ""
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")
            if not filename and not attachment_id:
                continue

            mime_type = part.get("mimeType") or "application/octet-stream"
            size = int(body.get("size") or 0)
            context = AttachmentContext(filename=filename or "(unnamed attachment)", mime_type=mime_type, size=size)

            if attachment_id and size <= max_bytes and self._is_model_readable_attachment(mime_type):
                raw = self._download_attachment(message_id, attachment_id)
                if raw:
                    if self._is_text_attachment(mime_type):
                        context.text_sample = raw.decode("utf-8", errors="ignore")[:4000]
                    else:
                        encoded = base64.b64encode(raw).decode("ascii")
                        context.data_url = f"data:{mime_type};base64,{encoded}"
            attachments.append(context)
        return attachments

    def _walk_parts(self, payload: dict[str, Any]):
        yield payload
        for part in payload.get("parts", []) or []:
            yield from self._walk_parts(part)

    def _download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        response = self._with_retry(
            self.service.users()
            .messages()
            .attachments()
            .get(userId=USER_ID, messageId=message_id, id=attachment_id)
            .execute
        )
        data = response.get("data", "")
        if not data:
            return b""
        padding = "=" * (-len(data) % 4)
        return base64.urlsafe_b64decode(data + padding)

    def _is_model_readable_attachment(self, mime_type: str) -> bool:
        return (
            self._is_text_attachment(mime_type)
            or mime_type in MODEL_FILE_MIME_TYPES
            or mime_type.startswith(MODEL_IMAGE_MIME_PREFIXES)
        )

    def _is_text_attachment(self, mime_type: str) -> bool:
        return mime_type.startswith(TEXT_ATTACHMENT_MIME_PREFIXES) or mime_type in TEXT_ATTACHMENT_MIME_TYPES
