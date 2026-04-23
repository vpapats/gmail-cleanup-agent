import pytest

from src.triage import TriageConfig


def test_invalid_mode_rejected():
    cfg = TriageConfig(
        mode="bad",
        use_model=False,
        min_trash_confidence=0.9,
        max_messages_per_run=10,
        max_trash_per_run=3,
        max_trash_per_sender=1,
        approved_trash_senders=set(),
        candidate_queries=[],
        labels={"protected": "p", "review": "r", "kept": "k", "trash_after_summary": "t"},
    )
    with pytest.raises(ValueError):
        cfg.validate()
