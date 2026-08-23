from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet, InvalidToken


STATE_FORMAT = "gmail-fomo-feedback-state"
STATE_VERSION = 1
DEFAULT_STATE_BRANCH = "gmail-fomo-state"
DEFAULT_STATE_PATH = ".gmail-fomo/feedback-state.enc.json"
GITHUB_API_VERSION = "2022-11-28"
MESSAGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


@dataclass(frozen=True)
class _RemoteState:
    message_ids: list[str]
    sha: str | None


class GitHubFeedbackStateStore:
    """Encrypted correction history persisted through the GitHub Contents API."""

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        encryption_key: str,
        branch: str = DEFAULT_STATE_BRANCH,
        path: str = DEFAULT_STATE_PATH,
        session: requests.Session | None = None,
        api_url: str = "https://api.github.com",
    ) -> None:
        if not token.strip():
            raise RuntimeError("GITHUB_TOKEN is required for feedback state")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository.strip()):
            raise RuntimeError("GITHUB_REPOSITORY must use owner/repository format")
        if not branch.strip():
            raise RuntimeError("GMAIL_FOMO_STATE_BRANCH must not be empty")
        if not path.strip() or path.startswith("/") or ".." in path.split("/"):
            raise RuntimeError("GMAIL_FOMO_STATE_PATH must be a safe relative path")
        try:
            self._fernet = Fernet(encryption_key.strip().encode("ascii"))
        except (ValueError, UnicodeEncodeError) as err:
            raise RuntimeError("GMAIL_FOMO_STATE_KEY is not a valid Fernet key") from err

        self.repository = repository.strip()
        self.branch = branch.strip()
        self.path = path.strip()
        self.api_url = api_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token.strip()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            }
        )
        self._loaded = False
        self._loaded_sha: str | None = None
        self._loaded_ids: list[str] = []

    @classmethod
    def from_env(cls) -> "GitHubFeedbackStateStore":
        return cls(
            token=_required_env("GITHUB_TOKEN"),
            repository=_required_env("GITHUB_REPOSITORY"),
            encryption_key=_required_env("GMAIL_FOMO_STATE_KEY"),
            branch=os.getenv("GMAIL_FOMO_STATE_BRANCH", DEFAULT_STATE_BRANCH),
            path=os.getenv("GMAIL_FOMO_STATE_PATH", DEFAULT_STATE_PATH),
        )

    def load(self) -> list[str]:
        remote = self._read_remote()
        self._loaded = True
        self._loaded_sha = remote.sha
        self._loaded_ids = list(remote.message_ids)
        return list(remote.message_ids)

    def save(self, message_ids: list[str]) -> None:
        if not self._loaded:
            raise RuntimeError("Feedback state must be loaded before it is saved")
        normalized = _normalize_message_ids(message_ids, reject_duplicates=False)
        if not set(self._loaded_ids).issubset(normalized):
            raise RuntimeError("Refusing to remove IDs from feedback state")

        envelope = self._encrypt(normalized)
        body: dict[str, Any] = {
            "message": "Update encrypted Gmail FOMO correction state",
            "content": base64.b64encode(envelope).decode("ascii"),
            "branch": self.branch,
        }
        if self._loaded_sha:
            body["sha"] = self._loaded_sha

        response = self.session.put(self._contents_url(), json=body, timeout=30)
        if response.status_code in {409, 422}:
            raise RuntimeError("Feedback state changed concurrently; no labels were finalized")
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"GitHub feedback state write failed with HTTP {response.status_code}"
            )

        verified = self._read_remote()
        if verified.message_ids != normalized:
            raise RuntimeError("GitHub feedback state failed remote read-back verification")
        self._loaded_sha = verified.sha
        self._loaded_ids = list(verified.message_ids)

    def _read_remote(self) -> _RemoteState:
        response = self.session.get(
            self._contents_url(),
            params={"ref": self.branch},
            timeout=30,
        )
        if response.status_code == 404:
            return _RemoteState(message_ids=[], sha=None)
        if response.status_code != 200:
            raise RuntimeError(
                f"GitHub feedback state read failed with HTTP {response.status_code}"
            )
        try:
            document = response.json()
            sha = document["sha"]
            encoded_content = document["content"].replace("\n", "")
            envelope = base64.b64decode(encoded_content, validate=True)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as err:
            raise RuntimeError("GitHub feedback state response is invalid") from err
        if not isinstance(sha, str) or not sha:
            raise RuntimeError("GitHub feedback state response has no valid SHA")
        return _RemoteState(message_ids=self._decrypt(envelope), sha=sha)

    def _encrypt(self, message_ids: list[str]) -> bytes:
        canonical_ids = _canonical_ids(message_ids)
        payload = {
            "version": STATE_VERSION,
            "message_ids": message_ids,
            "checksum": hashlib.sha256(canonical_ids).hexdigest(),
        }
        plaintext = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        envelope = {
            "format": STATE_FORMAT,
            "version": STATE_VERSION,
            "cipher": "fernet",
            "ciphertext": self._fernet.encrypt(plaintext).decode("ascii"),
        }
        return json.dumps(
            envelope,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _decrypt(self, envelope_bytes: bytes) -> list[str]:
        try:
            envelope = json.loads(envelope_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as err:
            raise RuntimeError("Encrypted feedback state envelope is invalid") from err
        if not isinstance(envelope, dict):
            raise RuntimeError("Encrypted feedback state envelope is invalid")
        if (
            envelope.get("format") != STATE_FORMAT
            or envelope.get("version") != STATE_VERSION
            or envelope.get("cipher") != "fernet"
            or not isinstance(envelope.get("ciphertext"), str)
        ):
            raise RuntimeError("Encrypted feedback state envelope is unsupported")
        try:
            plaintext = self._fernet.decrypt(envelope["ciphertext"].encode("ascii"))
            payload = json.loads(plaintext.decode("utf-8"))
        except (InvalidToken, UnicodeEncodeError, UnicodeDecodeError, json.JSONDecodeError) as err:
            raise RuntimeError("Encrypted feedback state authentication failed") from err
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            raise RuntimeError("Encrypted feedback state payload is unsupported")
        message_ids = _normalize_message_ids(
            payload.get("message_ids"),
            reject_duplicates=True,
        )
        checksum = payload.get("checksum")
        expected = hashlib.sha256(_canonical_ids(message_ids)).hexdigest()
        if not isinstance(checksum, str) or checksum != expected:
            raise RuntimeError("Encrypted feedback state checksum is invalid")
        return message_ids

    def _contents_url(self) -> str:
        encoded_path = quote(self.path, safe="/")
        return f"{self.api_url}/repos/{self.repository}/contents/{encoded_path}"


def _canonical_ids(message_ids: list[str]) -> bytes:
    return json.dumps(
        message_ids,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _normalize_message_ids(value: Any, *, reject_duplicates: bool) -> list[str]:
    if not isinstance(value, list):
        raise RuntimeError("Feedback state message IDs must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError("Feedback state contains an invalid message ID")
        message_id = item.strip()
        if not MESSAGE_ID_PATTERN.fullmatch(message_id):
            raise RuntimeError("Feedback state contains an invalid message ID")
        if message_id in seen:
            if reject_duplicates:
                raise RuntimeError("Feedback state contains duplicate message IDs")
            continue
        seen.add(message_id)
        normalized.append(message_id)
    return normalized


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value
