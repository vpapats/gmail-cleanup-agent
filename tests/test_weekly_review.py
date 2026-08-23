from datetime import date

import pytest
from cryptography.fernet import Fernet

from src.gmail_client import GmailMessageState
from src.weekly_auditor import DailyDecision, IndependentReview, WeekRange
from src.weekly_review import (
    PrivateReviewItem,
    ReviewItem,
    ReviewManifest,
    apply_review_manifest,
    build_review_manifest,
    decrypt_private_items,
    encrypt_private_items,
    _manifest_has_no_human_edits,
)


def _decision(message_id: str, label: str) -> DailyDecision:
    return DailyDecision(
        run_id=1,
        message_id=message_id,
        sender="sender@example.com",
        subject=f"Subject {message_id}",
        label=label,
        confidence=0.95,
        reason="reason",
    )


def _review(message_id: str, current: str, expected: str, certainty: str = "clear"):
    return IndependentReview(
        decision=_decision(message_id, current),
        expected_label=expected,
        certainty=certainty,
        evidence="specific evidence",
        important_attention=False,
        audit_confidence=0.95,
    )


def test_manifest_contains_only_noncorrect_items_and_preselects_current_label():
    manifest, private = build_review_manifest(
        WeekRange(date(2026, 8, 10), date(2026, 8, 17)),
        [
            _review("correct", "kept", "kept"),
            _review("wrong", "kept", "action_needed"),
            _review("unclear", "digest_and_trash", "kept", certainty="ambiguous"),
        ],
    )

    assert [item.current_label for item in manifest.items] == ["kept", "digest_and_trash"]
    assert [item.selected_label for item in manifest.items] == ["kept", "digest_and_trash"]
    assert [item.auditor_label for item in manifest.items] == ["action_needed", "kept"]
    assert len(private) == 2
    assert b"sender@example.com" not in manifest.to_json()


def test_private_mapping_is_encrypted_and_bound_to_review_id():
    key = Fernet.generate_key().decode()
    private = (
        PrivateReviewItem(
            "a" * 16,
            "gmail-id",
            "sender",
            "subject",
            "",
            "evidence",
            "kept",
            "action_needed",
            "clear",
            0.95,
        ),
    )
    encrypted = encrypt_private_items("weekly-2026-08-10-2026-08-17", private, key)

    assert b"gmail-id" not in encrypted
    restored = decrypt_private_items(
        encrypted, "weekly-2026-08-10-2026-08-17", key
    )
    assert restored[0].message_id == "gmail-id"
    assert restored[0].current_label == "kept"
    assert restored[0].sender == ""
    with pytest.raises(RuntimeError, match="does not match"):
        decrypt_private_items(encrypted, "weekly-2026-08-17-2026-08-24", key)


class _Gmail:
    names = {
        "AI/Kept": "kept-id",
        "AI/Action-Needed": "action-id",
        "AI/Digest-and-Trash": "digest-id",
    }

    def __init__(self, states):
        self.states = {key: set(value) for key, value in states.items()}
        self.replacements = []

    def get_existing_label_ids(self, names):
        assert set(names) == set(self.names)
        return dict(self.names)

    def get_message_state(self, message_id):
        return GmailMessageState(frozenset(self.states[message_id]), "h1")

    def replace_labels(self, message_id, *, add_label_ids, remove_label_ids):
        self.replacements.append((message_id, add_label_ids, remove_label_ids))
        self.states[message_id].difference_update(remove_label_ids)
        self.states[message_id].update(add_label_ids)


class _State:
    def __init__(self):
        self.loaded = 0
        self.records = []

    def load_records(self):
        self.loaded += 1
        return []

    def upsert_records(self, records):
        self.records = records


def _manifest(selected_second="action_needed"):
    return ReviewManifest(
        version=1,
        review_id="weekly-2026-08-10-2026-08-17",
        week_start="2026-08-10",
        week_end="2026-08-17",
        items=(
            ReviewItem("a" * 16, "kept", "kept", "action_needed", "clear", 0.95),
            ReviewItem(
                "b" * 16,
                "digest_and_trash",
                selected_second,
                "action_needed",
                "clear",
                0.96,
            ),
        ),
    )


def _private():
    return (
        PrivateReviewItem("a" * 16, "m1", "", "", "", "", "kept", "action_needed", "clear", 0.95),
        PrivateReviewItem("b" * 16, "m2", "", "", "", "", "digest_and_trash", "action_needed", "clear", 0.96),
    )


def test_apply_confirms_unchanged_changes_selected_and_learns_both():
    gmail = _Gmail({"m1": {"kept-id"}, "m2": {"digest-id"}})
    state = _State()

    ledger = apply_review_manifest(
        manifest=_manifest(),
        private_items=_private(),
        gmail=gmail,
        feedback_state=state,
        label_names={
            "kept": "AI/Kept",
            "action_needed": "AI/Action-Needed",
            "digest_and_trash": "AI/Digest-and-Trash",
        },
    )

    assert ledger["status"] == "complete"
    assert ledger["counts"]["confirmed"] == 1
    assert ledger["counts"]["changed"] == 1
    assert gmail.states["m2"] == {"action-id"}
    assert [(row.message_id, row.decision) for row in state.records] == [
        ("m1", "kept"),
        ("m2", "action_needed"),
    ]


def test_apply_aborts_all_changes_when_live_gmail_state_is_stale():
    gmail = _Gmail({"m1": set(), "m2": {"digest-id"}})
    state = _State()

    ledger = apply_review_manifest(
        manifest=_manifest(),
        private_items=_private(),
        gmail=gmail,
        feedback_state=state,
        label_names={
            "kept": "AI/Kept",
            "action_needed": "AI/Action-Needed",
            "digest_and_trash": "AI/Digest-and-Trash",
        },
    )

    assert ledger["status"] == "incomplete"
    assert ledger["counts"]["errors"] == 1
    assert gmail.replacements == []
    assert state.loaded == 0


def test_apply_rechecks_each_message_immediately_before_mutation():
    class ConcurrentGmail(_Gmail):
        def __init__(self):
            super().__init__({"m1": {"kept-id"}, "m2": {"digest-id"}})
            self.reads = 0

        def get_message_state(self, message_id):
            self.reads += 1
            if self.reads == 3 and message_id == "m1":
                self.states["m1"] = {"action-id"}
            return super().get_message_state(message_id)

    gmail = ConcurrentGmail()
    ledger = apply_review_manifest(
        manifest=_manifest(),
        private_items=_private(),
        gmail=gmail,
        feedback_state=_State(),
        label_names={
            "kept": "AI/Kept",
            "action_needed": "AI/Action-Needed",
            "digest_and_trash": "AI/Digest-and-Trash",
        },
    )

    assert ledger["status"] == "incomplete"
    assert ledger["results"][0]["result"] == "concurrent_gmail_change"
    assert gmail.replacements == []


def test_manifest_rejects_any_fourth_label():
    manifest = _manifest()
    bad = ReviewManifest(
        manifest.version,
        manifest.review_id,
        manifest.week_start,
        manifest.week_end,
        (
            ReviewItem("a" * 16, "kept", "retry", "kept", "clear", 0.9),
        ),
    )

    with pytest.raises(RuntimeError, match="three existing labels"):
        bad.validate()


def test_retry_may_refresh_only_a_manifest_without_human_edits():
    manifest = _manifest()
    assert _manifest_has_no_human_edits(manifest.to_json()) is False

    untouched = ReviewManifest(
        manifest.version,
        manifest.review_id,
        manifest.week_start,
        manifest.week_end,
        tuple(
            ReviewItem(
                item.item_id,
                item.current_label,
                item.current_label,
                item.auditor_label,
                item.certainty,
                item.auditor_confidence,
            )
            for item in manifest.items
        ),
    )
    assert _manifest_has_no_human_edits(untouched.to_json()) is True


def test_apply_rejects_changes_to_any_field_except_selected_label():
    manifest = _manifest()
    altered = ReviewManifest(
        manifest.version,
        manifest.review_id,
        manifest.week_start,
        manifest.week_end,
        (
            ReviewItem("a" * 16, "kept", "kept", "digest_and_trash", "clear", 0.95),
            manifest.items[1],
        ),
    )

    with pytest.raises(RuntimeError, match="Only selected_label"):
        apply_review_manifest(
            manifest=altered,
            private_items=_private(),
            gmail=_Gmail({"m1": {"kept-id"}, "m2": {"digest-id"}}),
            feedback_state=_State(),
            label_names={
                "kept": "AI/Kept",
                "action_needed": "AI/Action-Needed",
                "digest_and_trash": "AI/Digest-and-Trash",
            },
        )
