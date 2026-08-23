import pytest

from scripts.migrate_feedback_state import migrate_feedback_state


class _Gmail:
    def __init__(self, legacy_ids, *, delete_result=True):
        self.legacy_ids = legacy_ids
        self.delete_result = delete_result
        self.calls = []

    def load_legacy_feedback_message_ids(self):
        self.calls.append("load-draft")
        return list(self.legacy_ids)

    def delete_legacy_feedback_draft(self):
        self.calls.append("delete-draft")
        return self.delete_result


class _State:
    def __init__(self, ids, *, fail_save=False):
        self.ids = list(ids)
        self.fail_save = fail_save
        self.calls = []

    def load(self):
        self.calls.append("load-state")
        return list(self.ids)

    def save(self, ids):
        self.calls.append(("save-state", tuple(ids)))
        if self.fail_save:
            raise RuntimeError("remote verification failed")
        self.ids = list(ids)


def test_migration_verifies_state_before_deleting_draft():
    gmail = _Gmail(["m1", "m2"])
    state = _State(["m0"])

    result = migrate_feedback_state(gmail, state)

    assert result == (2, 3, True)
    assert state.calls == ["load-state", ("save-state", ("m0", "m1", "m2"))]
    assert gmail.calls == ["load-draft", "delete-draft"]


def test_migration_keeps_draft_when_state_write_or_verification_fails():
    gmail = _Gmail(["m1"])
    state = _State([], fail_save=True)

    with pytest.raises(RuntimeError, match="remote verification failed"):
        migrate_feedback_state(gmail, state)

    assert gmail.calls == ["load-draft"]


def test_migration_does_not_create_or_delete_any_draft_when_legacy_is_absent():
    gmail = _Gmail([])
    state = _State(["m0"])

    assert migrate_feedback_state(gmail, state) == (0, 1, False)
    assert gmail.calls == ["load-draft"]


def test_migration_fails_if_verified_legacy_draft_cannot_be_deleted():
    gmail = _Gmail(["m1"], delete_result=False)
    state = _State([])

    with pytest.raises(RuntimeError, match="disappeared"):
        migrate_feedback_state(gmail, state)
