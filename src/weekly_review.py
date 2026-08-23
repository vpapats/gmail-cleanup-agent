from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet, InvalidToken

from src.github_state import FeedbackStateRecord, GitHubFeedbackStateStore

if TYPE_CHECKING:
    from src.weekly_auditor import IndependentReview, WeekRange


ALLOWED_LABELS = {"kept", "action_needed", "digest_and_trash"}
MANIFEST_VERSION = 1
PRIVATE_FORMAT = "gmail-fomo-weekly-review-private"
REVIEW_ID_RE = re.compile(r"^weekly-\d{4}-\d{2}-\d{2}-\d{4}-\d{2}-\d{2}$")
ITEM_ID_RE = re.compile(r"^[a-f0-9]{16}$")


@dataclass(frozen=True)
class ReviewItem:
    item_id: str
    current_label: str
    selected_label: str
    auditor_label: str
    certainty: str
    auditor_confidence: float


@dataclass(frozen=True)
class PrivateReviewItem:
    item_id: str
    message_id: str
    sender: str
    subject: str
    received_at: str
    evidence: str
    current_label: str
    auditor_label: str
    certainty: str
    auditor_confidence: float


@dataclass(frozen=True)
class ReviewManifest:
    version: int
    review_id: str
    week_start: str
    week_end: str
    items: tuple[ReviewItem, ...]

    def validate(self) -> None:
        if self.version != MANIFEST_VERSION or not REVIEW_ID_RE.fullmatch(self.review_id):
            raise RuntimeError("Invalid weekly review manifest")
        try:
            start = date.fromisoformat(self.week_start)
            end = date.fromisoformat(self.week_end)
        except ValueError as err:
            raise RuntimeError("Invalid weekly review dates") from err
        if (
            end - start != timedelta(days=7)
            or self.review_id != f"weekly-{self.week_start}-{self.week_end}"
        ):
            raise RuntimeError("Weekly review dates do not match its ID")
        seen: set[str] = set()
        for item in self.items:
            if not ITEM_ID_RE.fullmatch(item.item_id) or item.item_id in seen:
                raise RuntimeError("Invalid or duplicate weekly review item ID")
            seen.add(item.item_id)
            if not {
                item.current_label,
                item.selected_label,
                item.auditor_label,
            }.issubset(ALLOWED_LABELS):
                raise RuntimeError("Weekly review accepts only the three existing labels")
            if item.certainty not in {"clear", "ambiguous"}:
                raise RuntimeError("Invalid weekly review certainty")
            if not 0 <= item.auditor_confidence <= 1:
                raise RuntimeError("Invalid weekly review confidence")

    def to_json(self) -> bytes:
        self.validate()
        document = {
            "version": self.version,
            "review_id": self.review_id,
            "week_start": self.week_start,
            "week_end": self.week_end,
            "instructions": (
                "Edit only selected_label. Allowed: kept, action_needed, digest_and_trash. "
                "Unchanged means the current label is confirmed."
            ),
            "items": [asdict(item) for item in self.items],
        }
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()


def build_review_manifest(
    week: WeekRange, reviews: list[IndependentReview]
) -> tuple[ReviewManifest, tuple[PrivateReviewItem, ...]]:
    review_id = f"weekly-{week.start.isoformat()}-{week.end.isoformat()}"
    public: list[ReviewItem] = []
    private: list[PrivateReviewItem] = []
    for review in reviews:
        if review.verdict == "correct":
            continue
        item_id = hashlib.sha256(
            f"{review_id}\0{review.decision.message_id}".encode()
        ).hexdigest()[:16]
        public.append(
            ReviewItem(
                item_id=item_id,
                current_label=review.decision.label,
                selected_label=review.decision.label,
                auditor_label=review.expected_label or review.decision.label,
                certainty=review.certainty,
                auditor_confidence=review.audit_confidence,
            )
        )
        private.append(
            PrivateReviewItem(
                item_id=item_id,
                message_id=review.decision.message_id,
                sender=review.decision.sender,
                subject=review.decision.subject,
                received_at=review.decision.received_at,
                evidence=review.evidence,
                current_label=review.decision.label,
                auditor_label=review.expected_label or review.decision.label,
                certainty=review.certainty,
                auditor_confidence=review.audit_confidence,
            )
        )
    manifest = ReviewManifest(
        MANIFEST_VERSION,
        review_id,
        week.start.isoformat(),
        week.end.isoformat(),
        tuple(public),
    )
    manifest.validate()
    return manifest, tuple(private)


def load_review_manifest(path: Path) -> ReviewManifest:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        manifest = ReviewManifest(
            version=data["version"],
            review_id=data["review_id"],
            week_start=data["week_start"],
            week_end=data["week_end"],
            items=tuple(ReviewItem(**item) for item in data["items"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
        raise RuntimeError("Weekly review manifest is invalid") from err
    manifest.validate()
    return manifest


def encrypt_private_items(
    review_id: str,
    items: tuple[PrivateReviewItem, ...],
    encryption_key: str,
) -> bytes:
    records = [_private_binding(item) for item in items]
    payload = {
        "version": MANIFEST_VERSION,
        "review_id": review_id,
        "items": records,
        "checksum": hashlib.sha256(_canonical(records)).hexdigest(),
    }
    envelope = {
        "format": PRIVATE_FORMAT,
        "version": MANIFEST_VERSION,
        "cipher": "fernet",
        "ciphertext": _fernet(encryption_key).encrypt(_canonical(payload)).decode(),
    }
    return _canonical(envelope)


def decrypt_private_items(
    data: bytes, review_id: str, encryption_key: str
) -> tuple[PrivateReviewItem, ...]:
    try:
        envelope = json.loads(data.decode())
        if (
            envelope.get("format") != PRIVATE_FORMAT
            or envelope.get("version") != MANIFEST_VERSION
            or envelope.get("cipher") != "fernet"
        ):
            raise RuntimeError("Unsupported encrypted weekly review")
        payload = json.loads(
            _fernet(encryption_key)
            .decrypt(envelope["ciphertext"].encode())
            .decode()
        )
        records = payload["items"]
        if payload.get("review_id") != review_id or payload.get("version") != MANIFEST_VERSION:
            raise RuntimeError("Encrypted weekly review does not match its manifest")
        if payload.get("checksum") != hashlib.sha256(_canonical(records)).hexdigest():
            raise RuntimeError("Encrypted weekly review checksum is invalid")
        items = tuple(
            PrivateReviewItem(
                item_id=item["item_id"],
                message_id=item["message_id"],
                sender="",
                subject="",
                received_at="",
                evidence="",
                current_label=item["current_label"],
                auditor_label=item["auditor_label"],
                certainty=item["certainty"],
                auditor_confidence=item["auditor_confidence"],
            )
            for item in records
        )
    except RuntimeError:
        raise
    except (InvalidToken, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Encrypted weekly review authentication failed") from err
    ids = [item.item_id for item in items]
    if len(ids) != len(set(ids)) or any(not ITEM_ID_RE.fullmatch(item) for item in ids):
        raise RuntimeError("Encrypted weekly review item IDs are invalid")
    return items


class GitHubReviewPublisher:
    def __init__(
        self,
        *,
        token: str,
        repository: str,
        encryption_key: str,
        base_branch: str = "main",
        session: requests.Session | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        if not token or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise RuntimeError("Valid GitHub token and owner/repository are required")
        _fernet(encryption_key)
        self.repository = repository
        self.owner = repository.split("/", 1)[0]
        self.base_branch = base_branch or "main"
        self.encryption_key = encryption_key
        self.api_url = api_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def publish(
        self, manifest: ReviewManifest, private_items: tuple[PrivateReviewItem, ...]
    ) -> str:
        manifest.validate()
        if not manifest.items:
            return ""
        if {item.item_id for item in private_items} != {
            item.item_id for item in manifest.items
        }:
            raise RuntimeError("Public and private weekly review items do not match")
        branch = f"gmail-fomo-review-{manifest.week_start}"
        self._ensure_branch(branch)
        private_path = f".gmail-fomo/reviews/{manifest.review_id}.enc.json"
        public_path = f"weekly_reviews/{manifest.review_id}.json"
        public_content = self._read_file(branch, public_path)
        if public_content is None or _manifest_has_no_human_edits(public_content):
            private_content = self._read_file(branch, private_path)
            existing_bindings = (
                [
                    _private_binding(item)
                    for item in decrypt_private_items(
                        private_content, manifest.review_id, self.encryption_key
                    )
                ]
                if private_content is not None
                else None
            )
            if existing_bindings != [
                _private_binding(item) for item in private_items
            ]:
                self._put_file(
                    branch,
                    private_path,
                    encrypt_private_items(
                        manifest.review_id, private_items, self.encryption_key
                    ),
                    "Add encrypted weekly Gmail review mapping",
                )
            self._put_file(
                branch,
                public_path,
                manifest.to_json(),
                "Add editable weekly Gmail review manifest",
            )
        elif self._read_file(branch, private_path) is None:
            raise RuntimeError("Weekly review branch is missing its encrypted mapping")
        response = self.session.get(
            f"{self.api_url}/repos/{self.repository}/pulls",
            params={"state": "all", "head": f"{self.owner}:{branch}", "base": self.base_branch},
            timeout=30,
        )
        _require(response, {200}, "Review PR lookup")
        if response.json():
            return str(response.json()[0]["html_url"])
        response = self.session.post(
            f"{self.api_url}/repos/{self.repository}/pulls",
            json={
                "title": f"Weekly Gmail review — {manifest.week_start}",
                "head": branch,
                "base": self.base_branch,
                "body": _pr_body(public_path),
            },
            timeout=30,
        )
        _require(response, {201}, "Review PR creation")
        return str(response.json()["html_url"])

    def _ensure_branch(self, branch: str) -> None:
        url = f"{self.api_url}/repos/{self.repository}/git/ref/heads/{quote(branch, safe='')}"
        response = self.session.get(url, timeout=30)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            _require(response, {200}, "Review branch lookup")
        response = self.session.get(
            f"{self.api_url}/repos/{self.repository}/git/ref/heads/{quote(self.base_branch, safe='')}",
            timeout=30,
        )
        _require(response, {200}, "Base branch lookup")
        response = self.session.post(
            f"{self.api_url}/repos/{self.repository}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": response.json()["object"]["sha"]},
            timeout=30,
        )
        _require(response, {201}, "Review branch creation")

    def _put_file(self, branch: str, path: str, content: bytes, message: str) -> None:
        url = f"{self.api_url}/repos/{self.repository}/contents/{quote(path, safe='/')}"
        existing = self.session.get(url, params={"ref": branch}, timeout=30)
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content).decode(),
            "branch": branch,
        }
        if existing.status_code == 200:
            current = base64.b64decode(existing.json()["content"].replace("\n", ""))
            if current == content:
                return
            body["sha"] = existing.json()["sha"]
        elif existing.status_code != 404:
            _require(existing, {200}, f"Review file lookup: {path}")
        response = self.session.put(url, json=body, timeout=30)
        _require(response, {200, 201}, f"Review file write: {path}")

    def _read_file(self, branch: str, path: str) -> bytes | None:
        url = f"{self.api_url}/repos/{self.repository}/contents/{quote(path, safe='/')}"
        response = self.session.get(url, params={"ref": branch}, timeout=30)
        if response.status_code == 404:
            return None
        _require(response, {200}, f"Review file lookup: {path}")
        try:
            return base64.b64decode(response.json()["content"].replace("\n", ""))
        except (KeyError, TypeError, ValueError) as err:
            raise RuntimeError(f"Review file response is invalid: {path}") from err


def build_private_review_html(
    manifest: ReviewManifest,
    private_items: tuple[PrivateReviewItem, ...],
    review_url: str,
) -> bytes:
    private = {item.item_id: item for item in private_items}
    rows: list[str] = []
    for item in manifest.items:
        detail = private[item.item_id]
        gmail_url = f"https://mail.google.com/mail/u/0/#all/{quote(detail.message_id, safe='')}"
        rows.append(
            "<tr>"
            f"<td><code>{item.item_id}</code></td>"
            f"<td><a href='{html.escape(gmail_url)}'>{html.escape(detail.subject or '(χωρίς θέμα)')}</a><br><small>{html.escape(detail.sender)}</small></td>"
            f"<td>{item.current_label}</td><td>{item.auditor_label}</td>"
            f"<td>{html.escape(detail.evidence)}</td></tr>"
        )
    page = f"""<!doctype html><html lang='el'><meta charset='utf-8'><title>Weekly Gmail review</title>
<style>body{{font:15px system-ui;max-width:1200px;margin:32px auto;padding:0 16px}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ddd;padding:8px;vertical-align:top}}th{{background:#f5f5f5}}</style>
<h1>Weekly Gmail review</h1><p>Έλεγξε το email και άλλαξε μόνο το <code>selected_label</code> του αντίστοιχου item στο <a href='{html.escape(review_url)}'>Review PR</a>. Επιλογές: kept, action_needed, digest_and_trash. Χωρίς αλλαγή σημαίνει επιβεβαίωση.</p>
<table><tr><th>Item</th><th>Email</th><th>Τρέχον</th><th>Auditor</th><th>Ένδειξη</th></tr>{''.join(rows)}</table></html>"""
    return page.encode()


def apply_review_manifest(
    *,
    manifest: ReviewManifest,
    private_items: tuple[PrivateReviewItem, ...],
    gmail: Any,
    feedback_state: GitHubFeedbackStateStore,
    label_names: dict[str, str],
) -> dict[str, Any]:
    manifest.validate()
    if set(label_names) != ALLOWED_LABELS:
        raise RuntimeError("Exactly the three existing Gmail labels are required")
    private = {item.item_id: item for item in private_items}
    if set(private) != {item.item_id for item in manifest.items}:
        raise RuntimeError("Public and encrypted review items do not match")
    for item in manifest.items:
        protected = private[item.item_id]
        if (
            item.current_label != protected.current_label
            or item.auditor_label != protected.auditor_label
            or item.certainty != protected.certainty
            or item.auditor_confidence != protected.auditor_confidence
        ):
            raise RuntimeError(
                "Only selected_label may change in a weekly review manifest"
            )
    existing = gmail.get_existing_label_ids(
        [label_names[key] for key in sorted(ALLOWED_LABELS)]
    )
    label_ids = {key: existing[value] for key, value in label_names.items()}
    classification_ids = frozenset(label_ids.values())
    preflight: list[tuple[ReviewItem, PrivateReviewItem, str]] = []
    conflicts: list[dict[str, str]] = []
    for item in manifest.items:
        detail = private[item.item_id]
        relevant = gmail.get_message_state(detail.message_id).label_ids.intersection(
            classification_ids
        )
        if relevant == {label_ids[item.selected_label]}:
            action = "confirmed" if item.selected_label == item.current_label else "already_applied"
            preflight.append((item, detail, action))
        elif relevant == {label_ids[item.current_label]}:
            preflight.append((item, detail, "change"))
        else:
            conflicts.append({"item_id": item.item_id, "result": "stale_gmail_state"})
    if conflicts:
        return _ledger(manifest, "incomplete", conflicts)

    results: list[dict[str, str]] = []
    for item, detail, _ in preflight:
        live = gmail.get_message_state(detail.message_id).label_ids.intersection(
            classification_ids
        )
        if live == {label_ids[item.selected_label]}:
            action = (
                "confirmed"
                if item.selected_label == item.current_label
                else "already_applied"
            )
        elif (
            live == {label_ids[item.current_label]}
            and item.selected_label != item.current_label
        ):
            action = "change"
        else:
            results.append(
                {"item_id": item.item_id, "result": "concurrent_gmail_change"}
            )
            return _ledger(manifest, "incomplete", results)
        if action == "change":
            try:
                gmail.replace_labels(
                    detail.message_id,
                    add_label_ids=[label_ids[item.selected_label]],
                    remove_label_ids=[
                        value for key, value in label_ids.items() if key != item.selected_label
                    ],
                )
                relevant = gmail.get_message_state(detail.message_id).label_ids.intersection(
                    classification_ids
                )
                if relevant != {label_ids[item.selected_label]}:
                    raise RuntimeError("Gmail label read-back failed")
                action = "changed"
            except Exception as err:
                results.append({"item_id": item.item_id, "result": "apply_failed", "error": type(err).__name__})
                return _ledger(manifest, "incomplete", results)
        results.append({"item_id": item.item_id, "result": action})

    try:
        feedback_state.load_records()
        feedback_state.upsert_records(
            [
                FeedbackStateRecord(
                    message_id=private[item.item_id].message_id,
                    decision=item.selected_label,
                    source=manifest.review_id,
                )
                for item in manifest.items
            ]
        )
    except Exception as err:
        results.append({"item_id": "feedback_state", "result": "learning_failed", "error": type(err).__name__})
        return _ledger(manifest, "incomplete", results)
    return _ledger(manifest, "complete", results)


def _ledger(manifest: ReviewManifest, status: str, results: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "version": 1,
        "review_id": manifest.review_id,
        "status": status,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "items": len(manifest.items),
            "changed": sum(row.get("result") == "changed" for row in results),
            "confirmed": sum(row.get("result") == "confirmed" for row in results),
            "already_applied": sum(row.get("result") == "already_applied" for row in results),
            "errors": sum(
                row.get("result")
                in {
                    "stale_gmail_state",
                    "concurrent_gmail_change",
                    "apply_failed",
                    "learning_failed",
                }
                for row in results
            ),
        },
        "results": results,
    }


def _pr_body(path: str) -> str:
    return (
        "No Gmail change occurs while this PR is open. Review the private HTML attached to the weekly email, "
        f"then edit only `selected_label` in `{path}`. Leaving it unchanged confirms the current label. "
        "The only values are `kept`, `action_needed`, and `digest_and_trash`. Merging approves Gmail application "
        "and controlled learning. Mailbox details remain encrypted in this public repository."
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _private_binding(item: PrivateReviewItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "message_id": item.message_id,
        "current_label": item.current_label,
        "auditor_label": item.auditor_label,
        "certainty": item.certainty,
        "auditor_confidence": item.auditor_confidence,
    }


def _manifest_has_no_human_edits(content: bytes) -> bool:
    try:
        document = json.loads(content.decode("utf-8"))
        items = document["items"]
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Existing weekly review manifest is invalid") from err
    if not isinstance(items, list):
        raise RuntimeError("Existing weekly review manifest is invalid")
    return all(
        isinstance(item, dict)
        and item.get("selected_label") == item.get("current_label")
        for item in items
    )


def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.strip().encode("ascii"))
    except (ValueError, UnicodeEncodeError) as err:
        raise RuntimeError("GMAIL_FOMO_STATE_KEY is not a valid Fernet key") from err


def _require(response: Any, statuses: set[int], action: str) -> None:
    if response.status_code not in statuses:
        raise RuntimeError(f"{action} failed with HTTP {response.status_code}")
