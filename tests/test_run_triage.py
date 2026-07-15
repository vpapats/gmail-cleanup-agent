import pytest

from scripts.run_triage import apply_manual_date_scope, apply_recheck_kept_scope, load_config


def test_manual_date_scope_keeps_existing_safety_query_and_adds_dates():
    config = load_config("config/settings.yaml")
    assert config.min_trash_confidence == 0.85

    scoped = apply_manual_date_scope(
        config.candidate_queries,
        date_from="2026-06-01",
        date_to="2026-07-01",
    )

    assert len(scoped) == len(config.candidate_queries)
    assert scoped[0].startswith(config.candidate_queries[0])
    assert "after:2026/6/1" in scoped[0]
    assert "before:2026/7/1" in scoped[0]
    assert "-label:AI/FOMO-Summarized" in scoped[0]
    assert "-subject:\"Today's GMAIL FOMO summary\"" in scoped[0]


def test_manual_date_scope_is_noop_without_dates():
    queries = ["in:inbox -label:AI/Digest-and-Trash"]

    assert apply_manual_date_scope(queries) == queries


def test_manual_date_scope_rejects_ambiguous_dates():
    with pytest.raises(SystemExit):
        apply_manual_date_scope(["in:inbox"], date_from="06/01/2026")


def test_manual_recheck_kept_removes_only_kept_exclusion():
    config = load_config("config/settings.yaml")

    queries = apply_recheck_kept_scope(
        config.candidate_queries,
        kept_label=config.labels["kept"],
        enabled=True,
    )

    assert "-label:AI/Kept" not in queries[0]
    assert "-label:AI/Action-Needed" in queries[0]
    assert "-label:AI/Digest-and-Trash" in queries[0]
    assert "-subject:\"Today's GMAIL FOMO summary\"" in queries[0]
