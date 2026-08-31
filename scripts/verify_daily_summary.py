from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import time as time_module
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.gmail_client import GmailClient


ATHENS = ZoneInfo("Europe/Athens")
SUBJECT_PREFIX = "Today's GMAIL FOMO summary"
NUMBERED_EMAIL_RE = re.compile(
    r"^\d+\.\s+\S.*?(?=^\d+\.\s+\S|\Z)",
    re.MULTILINE | re.DOTALL,
)
RECEIVED_RE = re.compile(r"^Received:\s*(\S+)\s*$", re.MULTILINE)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class SummaryVerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class SummaryVerification:
    message_id: str
    referenced_emails: int
    received_dates: int


def _decode_body(payload: dict[str, Any]) -> str:
    body = payload.get("body") or {}
    data = body.get("data") if isinstance(body, dict) else None
    if data:
        try:
            padding = "=" * (-len(data) % 4)
            return base64.urlsafe_b64decode(data + padding).decode(
                "utf-8", errors="replace"
            )
        except (TypeError, ValueError, binascii.Error) as err:
            raise SummaryVerificationError("Daily summary body is invalid") from err
    for part in payload.get("parts") or []:
        if isinstance(part, dict):
            text = _decode_body(part)
            if text:
                return text
    return ""


def _execute_with_retry(
    request: Any,
    *,
    operation: str,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    delay = 1.0
    for attempt in range(5):
        try:
            response = request.execute()
            if not isinstance(response, dict):
                raise SummaryVerificationError(
                    f"Gmail {operation} returned an invalid response"
                )
            return response
        except SummaryVerificationError:
            raise
        except Exception as err:
            status = getattr(getattr(err, "resp", None), "status", None)
            if status in TRANSIENT_HTTP_STATUSES and attempt < 4:
                sleep(delay)
                delay *= 2
                continue
            detail = f"HTTP {status}" if status else "an unexpected error"
            raise SummaryVerificationError(
                f"Gmail {operation} failed with {detail}"
            ) from err
    raise AssertionError("unreachable")


def _search_candidate_ids(
    service: Any,
    subject: str,
    *,
    sleep: Callable[[float], None],
) -> list[str]:
    result: list[str] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    while True:
        kwargs: dict[str, Any] = {
            "userId": "me",
            # Gmail's default search scope omits Trash even when a message still
            # carries the immutable SENT label. Search everywhere, then keep the
            # explicit SENT-label check below so a later user move to Trash does
            # not turn a successful delivery into a false missing-summary alert.
            "q": f'in:anywhere label:sent subject:"{subject}"',
            "maxResults": 100,
        }
        if page_token:
            kwargs["pageToken"] = page_token
        response = _execute_with_retry(
            service.users().messages().list(**kwargs),
            operation="summary search",
            sleep=sleep,
        )
        for candidate in response.get("messages") or []:
            message_id = candidate.get("id") if isinstance(candidate, dict) else None
            if message_id:
                result.append(str(message_id))
        next_token = response.get("nextPageToken")
        if not next_token:
            return list(dict.fromkeys(result))
        next_token = str(next_token)
        if next_token in seen_tokens:
            raise SummaryVerificationError("Gmail summary search repeated a page token")
        seen_tokens.add(next_token)
        page_token = next_token


def _exact_summaries(
    service: Any,
    subject: str,
    *,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    exact: list[dict[str, Any]] = []
    for message_id in _search_candidate_ids(service, subject, sleep=sleep):
        message = _execute_with_retry(
            service.users().messages().get(
                userId="me",
                id=message_id,
                format="full",
            ),
            operation=f"summary read for message {message_id}",
            sleep=sleep,
        )
        payload = message.get("payload") or {}
        headers = {
            str(item.get("name") or "").lower(): str(item.get("value") or "")
            for item in payload.get("headers") or []
            if isinstance(item, dict)
        }
        if headers.get("subject") == subject:
            exact.append(message)
    return exact


def verify_daily_summary(
    service: Any,
    target_date: date,
    *,
    sleep: Callable[[float], None] = time_module.sleep,
) -> SummaryVerification:
    subject = f"{SUBJECT_PREFIX} - {target_date.isoformat()}"
    exact: list[dict[str, Any]] = []
    for attempt in range(6):
        exact = _exact_summaries(service, subject, sleep=sleep)
        if exact or attempt == 5:
            break
        sleep(10)
    if len(exact) != 1:
        raise SummaryVerificationError(
            f"Expected exactly one sent daily summary for {target_date.isoformat()}; "
            f"found {len(exact)}"
        )
    message = exact[0]
    if "SENT" not in (message.get("labelIds") or []):
        raise SummaryVerificationError("Daily summary is not labeled SENT")
    try:
        sent_at = datetime.fromtimestamp(
            int(message.get("internalDate") or "") / 1000,
            tz=timezone.utc,
        )
    except (TypeError, ValueError, OverflowError) as err:
        raise SummaryVerificationError("Daily summary internalDate is invalid") from err
    if sent_at.astimezone(ATHENS).date() != target_date:
        raise SummaryVerificationError("Daily summary was not sent on the target Athens date")
    payload = message.get("payload") or {}
    body = _decode_body(payload)
    if not body:
        raise SummaryVerificationError("Daily summary body is empty")
    entries = NUMBERED_EMAIL_RE.findall(body)
    received_dates: list[str] = []
    for index, entry in enumerate(entries, start=1):
        received = RECEIVED_RE.findall(entry)
        if len(received) != 1:
            raise SummaryVerificationError(
                f"Daily summary entry {index} has {len(received)} receipt dates; expected 1"
            )
        value = received[0]
        try:
            parsed = date.fromisoformat(value)
        except ValueError as err:
            raise SummaryVerificationError(
                f"Daily summary entry {index} has an invalid receipt date"
            ) from err
        if not ISO_DATE_RE.fullmatch(value) or parsed.isoformat() != value:
            raise SummaryVerificationError(
                f"Daily summary entry {index} has an invalid receipt date"
            )
        received_dates.append(value)
    referenced = len(entries)
    if referenced == 0 and "No digest-and-trash emails needed a summary today." not in body:
        raise SummaryVerificationError("Daily summary has no verifiable email entries")
    return SummaryVerification(
        message_id=str(message.get("id") or ""),
        referenced_emails=referenced,
        received_dates=len(received_dates),
    )


def emit(
    result: SummaryVerification,
    *,
    target_date: date,
    output_path: str,
    summary_path: str,
) -> None:
    values = {
        "gmail_summary_verified": "true",
        "referenced_emails": str(result.referenced_emails),
        "received_dates": str(result.received_dates),
    }
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(
                f"- Gmail read-back: **verified** for {target_date.isoformat()}; "
                f"SENT and {result.received_dates}/{result.referenced_emails} receipt dates present.\n"
            )
    print(json.dumps(values))


def emit_failure(error: Exception, *, target_date: date, summary_path: str) -> None:
    message = " ".join(str(error).splitlines())
    print(f"::error::{message}")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(
                f"- Gmail read-back: **failed** for {target_date.isoformat()}: {message}\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    try:
        try:
            client = GmailClient()
        except Exception as err:
            raise SummaryVerificationError("Gmail authentication failed") from err
        result = verify_daily_summary(client.service, args.date)
        emit(
            result,
            target_date=args.date,
            output_path=os.getenv("GITHUB_OUTPUT", ""),
            summary_path=os.getenv("GITHUB_STEP_SUMMARY", ""),
        )
    except SummaryVerificationError as err:
        emit_failure(
            err,
            target_date=args.date,
            summary_path=os.getenv("GITHUB_STEP_SUMMARY", ""),
        )
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
