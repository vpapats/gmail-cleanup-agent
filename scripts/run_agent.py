"""
Entrypoint for the Gmail cleanup agent.

This script searches the inbox for low-value informational emails, applies hard
protection rules, classifies allowed candidates with an OpenAI-compatible LLM,
logs the outcome to Google Sheets, and either labels or trashes threads
depending on configuration.

Required environment variables:
- GOOGLE_CLIENT_ID
- GOOGLE_CLIENT_SECRET
- GOOGLE_REFRESH_TOKEN
- LLM_API_KEY
- SHEET_ID

Optional environment variables:
- LLM_MODEL
- LLM_API_BASE
- SHEET_TAB_NAME
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Make repo root importable when running: python scripts/run_agent.py
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config.config import (  # noqa: E402
    CANDIDATE_QUERIES,
    LABELS,
    MODES,
    PROTECTED_DOMAINS,
    PROTECTED_SENDERS,
    THRESHOLDS,
    TIMEZONE,
    TRASH_LANE_SENDERS,
)

GMAIL_SCOPE = "https://mail.google.com/"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
USER_ID = "me"

LOG_SHEET_NAME = os.environ.get("SHEET_TAB_NAME", "gmail_agent_log")
LOG_HEADERS = [
    "run_ts",
    "thread_id",
    "message_id",
    "sender",
    "subject",
    "internal_date",
    "bucket_name",
    "decision",
    "confidence",
    "reason_short",
    "summary_1l",
    "protected_hits_json",
    "action_taken",
    "trash_ts",
    "error",
    "raw_decision_json",
]

ALLOWED_DECISIONS = {"keep", "review", "summarize_then_trash"}


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def now_iso() -> str:
    try:
        tz = ZoneInfo(TIMEZONE)
    except Exception:
        tz = dt.timezone.utc
    return dt.datetime.now(tz).isoformat()


def build_credentials() -> Credentials:
    """
    Build Google credentials from environment variables.

    Important:
    The refresh token must have been granted BOTH Gmail and Sheets scopes.
    """
    creds = Credentials(
        token=None,
        refresh_token=require_env("GOOGLE_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=require_env("GOOGLE_CLIENT_ID"),
        client_secret=require_env("GOOGLE_CLIENT_SECRET"),
        scopes=[GMAIL_SCOPE, SHEETS_SCOPE],
    )
    creds.refresh(Request())
    return creds


def build_services() -> tuple[Any, Any]:
    creds = build_credentials()
    gmail_service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    sheets_service = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return gmail_service, sheets_service


def header_map(message: Dict[str, Any]) -> Dict[str, str]:
    headers = (
        message.get("payload", {}).get("headers", [])
        if isinstance(message.get("payload"), dict)
        else []
    )
    out: Dict[str, str] = {}
    for item in headers:
        name = str(item.get("name", "")).strip().lower()
        value = str(item.get("value", "")).strip()
        if name and value:
            out[name] = value
    return out


def extract_email(addr: str) -> str:
    addr = (addr or "").strip()
    if "<" in addr and ">" in addr:
        return addr.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return addr.lower()


def domain_of(email: str) -> str:
    return email.split("@", 1)[1].lower() if "@" in email else ""


def payload_has_attachments(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False

    filename = str(payload.get("filename", "")).strip()
    body = payload.get("body", {}) or {}
    if filename:
        return True
    if body.get("attachmentId"):
        return True

    for part in payload.get("parts", []) or []:
        if payload_has_attachments(part):
            return True
    return False


def latest_message(thread: Dict[str, Any]) -> Dict[str, Any]:
    messages = thread.get("messages", []) or []
    if not messages:
        raise RuntimeError("Thread has no messages")
    return max(messages, key=lambda m: int(m.get("internalDate", "0")))


def safe_snippet(message: Dict[str, Any]) -> str:
    return str(message.get("snippet", "")).strip()


def ensure_label(gmail_service: Any, label_name: str) -> str:
    results = gmail_service.users().labels().list(userId=USER_ID).execute()
    for label in results.get("labels", []) or []:
        if label.get("name") == label_name:
            return str(label["id"])

    body = {
        "name": label_name,
        "labelListVisibility": "labelShow",
        "messageListVisibility": "show",
    }
    created = gmail_service.users().labels().create(userId=USER_ID, body=body).execute()
    return str(created["id"])


def ensure_labels(gmail_service: Any) -> Dict[str, str]:
    label_ids: Dict[str, str] = {}
    for key, label_name in LABELS.items():
        label_ids[key] = ensure_label(gmail_service, label_name)
    return label_ids


def add_label_to_thread(gmail_service: Any, thread_id: str, label_id: str) -> None:
    gmail_service.users().threads().modify(
        userId=USER_ID,
        id=thread_id,
        body={"addLabelIds": [label_id]},
    ).execute()


def trash_thread(gmail_service: Any, thread_id: str) -> None:
    gmail_service.users().threads().trash(userId=USER_ID, id=thread_id).execute()


def ensure_log_sheet(sheets_service: Any, sheet_id: str) -> None:
    spreadsheet = sheets_service.spreadsheets().get(spreadsheetId=sheet_id).execute()
    existing_titles = {
        sheet.get("properties", {}).get("title", "")
        for sheet in spreadsheet.get("sheets", []) or []
    }

    if LOG_SHEET_NAME not in existing_titles:
        sheets_service.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": LOG_SHEET_NAME,
                            }
                        }
                    }
                ]
            },
        ).execute()

    first_row = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{LOG_SHEET_NAME}!1:1",
    ).execute()

    values = first_row.get("values", [])
    if not values:
        sheets_service.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{LOG_SHEET_NAME}!A1",
            valueInputOption="RAW",
            body={"values": [LOG_HEADERS]},
        ).execute()


def append_log_row(sheets_service: Any, sheet_id: str, row: List[Any]) -> None:
    sheets_service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{LOG_SHEET_NAME}!A:A",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def list_candidate_thread_ids(gmail_service: Any, query: str, max_total: int = 50) -> List[str]:
    thread_ids: List[str] = []
    page_token: str | None = None

    while len(thread_ids) < max_total:
        response = gmail_service.users().threads().list(
            userId=USER_ID,
            q=query,
            maxResults=min(100, max_total - len(thread_ids)),
            pageToken=page_token,
        ).execute()

        for item in response.get("threads", []) or []:
            thread_id = str(item.get("id", "")).strip()
            if thread_id:
                thread_ids.append(thread_id)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return thread_ids


def thread_protected(
    sender_email: str,
    subject: str,
    snippet: str,
    has_attachments: bool,
    message_count: int,
) -> Tuple[bool, List[str]]:
    hits: List[str] = []

    if sender_email in {s.lower() for s in PROTECTED_SENDERS}:
        hits.append("protected_sender")

    sender_domain = domain_of(sender_email)
    if sender_domain in {d.lower() for d in PROTECTED_DOMAINS}:
        hits.append("protected_domain")

    if has_attachments:
        hits.append("has_attachment")

    if message_count > 1:
        hits.append("reply_thread")

    haystack_subject = (subject or "").lower()
    haystack_snippet = (snippet or "").lower()

    keywords = [
        "invoice",
        "receipt",
        "payment",
        "contract",
        "quote",
        "signature",
        "expires",
        "renewal",
        "form submission",
        "lead",
        "appointment",
        "inspection",
        "permit",
        "tax",
        "δήλωση",
        "τιμολόγιο",
        "προσφορά",
        "υπογραφή",
        "ραντεβού",
    ]
    for kw in keywords:
        if kw in haystack_subject or kw in haystack_snippet:
            hits.append(f"term:{kw}")

    return (len(hits) > 0, hits)


def build_classifier_payload(
    thread: Dict[str, Any],
    latest: Dict[str, Any],
    bucket_name: str,
) -> Dict[str, Any]:
    headers = header_map(latest)
    return {
        "bucket": bucket_name,
        "thread_id": thread.get("id"),
        "message_count": len(thread.get("messages", []) or []),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "has_attachments": payload_has_attachments(latest.get("payload", {})),
        "label_ids": latest.get("labelIds", []) or [],
        "snippet": safe_snippet(latest),
    }


def normalize_decision(raw: Dict[str, Any]) -> Dict[str, Any]:
    decision = str(raw.get("decision", "review")).strip().lower()
    if decision not in ALLOWED_DECISIONS:
        decision = "review"

    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    return {
        "decision": decision,
        "confidence": confidence,
        "category": str(raw.get("category", "")).strip(),
        "reason_short": str(raw.get("reason_short", "")).strip(),
        "summary_1l": str(raw.get("summary_1l", "")).strip(),
        "protected_hits": raw.get("protected_hits", []),
        "action_signals": raw.get("action_signals", []),
    }


def call_classifier(payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = require_env("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL", "gpt-4o-mini")
    api_base = os.environ.get("LLM_API_BASE", "https://api.openai.com/v1").rstrip("/")
    url = f"{api_base}/chat/completions"

    system_prompt = (
        "You are a Gmail cleanup classifier.\n"
        "Allowed decisions: keep, review, summarize_then_trash.\n"
        "Hard rules:\n"
        "- If there is any sign of direct human communication, attachments, invoices, receipts,\n"
        "  legal/admin/compliance, leads, appointments, travel, medical, signatures, deadlines,\n"
        "  or personal relevance, do NOT choose summarize_then_trash.\n"
        "- If uncertain, choose review.\n"
        "- summarize_then_trash is only for low-value informational emails such as newsletters,\n"
        "  market briefings, webinar promos, generic no-reply updates, and promotional blasts.\n"
        "Return ONLY a JSON object with fields:\n"
        "decision, confidence, category, reason_short, summary_1l, protected_hits, action_signals."
    )

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }

    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=90,
    )

    if response.status_code != 200:
        raise RuntimeError(f"LLM request failed ({response.status_code}): {response.text}")

    data = response.json()
    content = data["choices"][0]["message"]["content"].strip()

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"LLM returned invalid JSON: {content}") from exc

    return normalize_decision(parsed)


def process_thread(
    gmail_service: Any,
    sheets_service: Any,
    sheet_id: str,
    label_ids: Dict[str, str],
    bucket_name: str,
    thread_id: str,
    counters: Dict[str, Any],
) -> None:
    run_ts = counters["run_ts"]

    thread = gmail_service.users().threads().get(
        userId=USER_ID,
        id=thread_id,
        format="full",
    ).execute()

    latest = latest_message(thread)
    headers = header_map(latest)
    sender_header = headers.get("from", "")
    sender_email = extract_email(sender_header)
    subject = headers.get("subject", "")
    snippet = safe_snippet(latest)
    internal_date = latest.get("internalDate", "")
    message_id = latest.get("id", "")
    has_attachments = payload_has_attachments(latest.get("payload", {}))
    message_count = len(thread.get("messages", []) or [])

    protected, protection_hits = thread_protected(
        sender_email=sender_email,
        subject=subject,
        snippet=snippet,
        has_attachments=has_attachments,
        message_count=message_count,
    )

    if protected:
        add_label_to_thread(gmail_service, thread_id, label_ids["PROTECTED"])
        append_log_row(
            sheets_service,
            sheet_id,
            [
                run_ts,
                thread_id,
                message_id,
                sender_header,
                subject,
                internal_date,
                bucket_name,
                "keep",
                1.0,
                "Hard protection rule hit",
                "",
                json.dumps(protection_hits, ensure_ascii=False),
                "protected",
                "",
                "",
                "",
            ],
        )
        return

    if sender_email.lower() not in {s.lower() for s in TRASH_LANE_SENDERS}:
        add_label_to_thread(gmail_service, thread_id, label_ids["KEPT"])
        append_log_row(
            sheets_service,
            sheet_id,
            [
                run_ts,
                thread_id,
                message_id,
                sender_header,
                subject,
                internal_date,
                bucket_name,
                "keep",
                1.0,
                "Sender not in trash lane",
                "",
                "[]",
                "kept",
                "",
                "",
                "",
            ],
        )
        return

    classifier_payload = build_classifier_payload(thread, latest, bucket_name)
    decision = call_classifier(classifier_payload)

    action_taken = "kept"
    trash_ts = ""
    error_text = ""

    if decision["decision"] == "summarize_then_trash":
        if decision["confidence"] < float(THRESHOLDS["AUTO_TRASH_CONFIDENCE"]):
            add_label_to_thread(gmail_service, thread_id, label_ids["REVIEW"])
            action_taken = "review_low_confidence"

        elif int(counters["total_trashed"]) >= int(THRESHOLDS["MAX_TRASH_THREADS_PER_RUN"]):
            add_label_to_thread(gmail_service, thread_id, label_ids["REVIEW"])
            action_taken = "review_total_quota_reached"

        elif int(counters["sender_trash_counts"].get(sender_email, 0)) >= int(
            THRESHOLDS["MAX_TRASH_THREADS_PER_SENDER"]
        ):
            add_label_to_thread(gmail_service, thread_id, label_ids["REVIEW"])
            action_taken = "review_sender_quota_reached"

        elif bool(MODES["SHADOW_MODE"]) or not bool(MODES["ENABLE_TRASH"]):
            add_label_to_thread(gmail_service, thread_id, label_ids["TRASHED"])
            action_taken = "shadow_trash_candidate"

        else:
            add_label_to_thread(gmail_service, thread_id, label_ids["TRASHED"])
            trash_thread(gmail_service, thread_id)
            trash_ts = now_iso()
            counters["total_trashed"] += 1
            counters["sender_trash_counts"][sender_email] = (
                counters["sender_trash_counts"].get(sender_email, 0) + 1
            )
            action_taken = "trashed"

    elif decision["decision"] == "review":
        add_label_to_thread(gmail_service, thread_id, label_ids["REVIEW"])
        action_taken = "review"

    else:
        add_label_to_thread(gmail_service, thread_id, label_ids["KEPT"])
        action_taken = "kept"

    append_log_row(
        sheets_service,
        sheet_id,
        [
            run_ts,
            thread_id,
            message_id,
            sender_header,
            subject,
            internal_date,
            bucket_name,
            decision["decision"],
            decision["confidence"],
            decision["reason_short"],
            decision["summary_1l"],
            json.dumps(decision.get("protected_hits", []), ensure_ascii=False),
            action_taken,
            trash_ts,
            error_text,
            json.dumps(decision, ensure_ascii=False),
        ],
    )


def main() -> None:
    gmail_service, sheets_service = build_services()
    sheet_id = require_env("SHEET_ID")

    ensure_log_sheet(sheets_service, sheet_id)
    label_ids = ensure_labels(gmail_service)

    counters: Dict[str, Any] = {
        "run_ts": now_iso(),
        "total_trashed": 0,
        "sender_trash_counts": {},
    }

    seen_thread_ids: set[str] = set()

    for bucket in CANDIDATE_QUERIES:
        bucket_name = str(bucket.get("name", "")).strip() or "unnamed_bucket"
        query = str(bucket.get("query", "")).strip()
        if not query:
            continue

        thread_ids = list_candidate_thread_ids(gmail_service, query=query, max_total=50)

        for thread_id in thread_ids:
            if thread_id in seen_thread_ids:
                continue
            seen_thread_ids.add(thread_id)

            try:
                process_thread(
                    gmail_service=gmail_service,
                    sheets_service=sheets_service,
                    sheet_id=sheet_id,
                    label_ids=label_ids,
                    bucket_name=bucket_name,
                    thread_id=thread_id,
                    counters=counters,
                )
            except Exception as exc:
                append_log_row(
                    sheets_service,
                    sheet_id,
                    [
                        counters["run_ts"],
                        thread_id,
                        "",
                        "",
                        "",
                        "",
                        bucket_name,
                        "error",
                        0.0,
                        "",
                        "",
                        "[]",
                        "error",
                        "",
                        str(exc),
                        "",
                    ],
                )

            time.sleep(0.15)


if __name__ == "__main__":
    main()
