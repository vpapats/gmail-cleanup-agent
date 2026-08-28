import base64
import json

import pytest
from cryptography.fernet import Fernet

from src.github_state import FeedbackStateRecord, GitHubFeedbackStateStore


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Session:
    def __init__(self):
        self.headers = {}
        self.sha = None
        self.envelope = None
        self.put_calls = []

    def get(self, url, *, params, timeout):
        assert params == {"ref": "gmail-fomo-state"}
        assert timeout == 30
        if self.sha is None:
            return _Response(404)
        return _Response(
            200,
            {
                "sha": self.sha,
                "content": base64.b64encode(self.envelope).decode("ascii"),
            },
        )

    def put(self, url, *, json, timeout):
        assert timeout == 30
        self.put_calls.append(json)
        if self.sha is None and "sha" in json:
            return _Response(422)
        if self.sha is not None and json.get("sha") != self.sha:
            return _Response(409)
        self.envelope = base64.b64decode(json["content"])
        self.sha = "sha-2" if self.sha else "sha-1"
        return _Response(200 if "sha" in json else 201)


def _store(session=None):
    return GitHubFeedbackStateStore(
        token="token",
        repository="owner/repo",
        encryption_key=Fernet.generate_key().decode("ascii"),
        session=session or _Session(),
    )


def test_state_create_is_encrypted_and_verified_without_plaintext_ids():
    session = _Session()
    store = _store(session)

    assert store.load() == []
    store.save(["18abc123", "18def456"])

    assert b"18abc123" not in session.envelope
    assert b"18def456" not in session.envelope
    public_envelope = json.loads(session.envelope)
    assert set(public_envelope) == {"cipher", "ciphertext", "format", "version"}
    assert store.load() == ["18abc123", "18def456"]
    assert "sha" not in session.put_calls[0]


def test_state_update_uses_loaded_sha_and_preserves_history():
    session = _Session()
    store = _store(session)
    store.load()
    store.save(["m1"])

    store.load()
    store.save(["m1", "m2"])

    assert session.put_calls[-1]["sha"] == "sha-1"
    assert store.load() == ["m1", "m2"]
    with pytest.raises(RuntimeError, match="Refusing to remove"):
        store.save(["m2"])


def test_state_conflict_aborts_without_overwriting():
    session = _Session()
    store = _store(session)
    store.load()
    store.save(["m1"])
    store.load()
    session.sha = "external-sha"

    with pytest.raises(RuntimeError, match="changed concurrently"):
        store.save(["m1", "m2"])


def test_state_rejects_tampered_ciphertext():
    session = _Session()
    store = _store(session)
    store.load()
    store.save(["m1"])
    envelope = json.loads(session.envelope)
    envelope["ciphertext"] = "gAAAA-invalid"
    session.envelope = json.dumps(envelope).encode("utf-8")

    with pytest.raises(RuntimeError, match="authentication failed"):
        store.load()


def test_state_rejects_validly_encrypted_bad_checksum_and_duplicates():
    session = _Session()
    store = _store(session)
    payload = {
        "version": 1,
        "message_ids": ["m1", "m1"],
        "checksum": "wrong",
    }
    envelope = {
        "format": "gmail-fomo-feedback-state",
        "version": 1,
        "cipher": "fernet",
        "ciphertext": store._fernet.encrypt(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii"),
    }
    session.envelope = json.dumps(envelope).encode("utf-8")
    session.sha = "sha-1"

    with pytest.raises(RuntimeError, match="duplicate"):
        store.load()


def test_state_requires_load_before_save():
    store = _store()

    with pytest.raises(RuntimeError, match="must be loaded"):
        store.save(["m1"])


def test_v2_records_preserve_approved_decisions_and_replace_prior_answer():
    session = _Session()
    store = _store(session)
    assert store.load_records() == []

    store.upsert_records(
        [
            FeedbackStateRecord("m1", "action_needed", "weekly-2026-08-10-2026-08-17"),
            FeedbackStateRecord("m2", "kept", "legacy-feedback"),
        ]
    )
    assert store.load_records() == [
        FeedbackStateRecord("m1", "action_needed", "weekly-2026-08-10-2026-08-17"),
        FeedbackStateRecord("m2", "kept", "legacy-feedback"),
    ]

    store.upsert_records(
        [FeedbackStateRecord("m1", "digest_and_trash", "weekly-2026-08-17-2026-08-24")]
    )
    assert store.load_records()[0].decision == "digest_and_trash"


def test_v1_state_is_read_as_kept_records_and_migrates_on_next_write():
    session = _Session()
    store = _store(session)
    ids = ["m1"]
    payload = {
        "version": 1,
        "message_ids": ids,
        "checksum": __import__("hashlib").sha256(b'["m1"]').hexdigest(),
    }
    envelope = {
        "format": "gmail-fomo-feedback-state",
        "version": 1,
        "cipher": "fernet",
        "ciphertext": store._fernet.encrypt(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).decode(),
    }
    session.envelope = json.dumps(envelope).encode()
    session.sha = "sha-v1"

    assert store.load_records() == [FeedbackStateRecord("m1")]
    store.upsert_records([FeedbackStateRecord("m2", "action_needed", "weekly-test")])
    assert json.loads(session.envelope)["version"] == 2
