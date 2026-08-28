from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts.daily_schedule_gate import decide, find_prior_started_triage, scheduled_slot


REPOSITORY = "vpapats/gmail-cleanup-agent"


def _now(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _runs(*runs):
    return {"workflow_runs": list(runs)}


def _jobs(*steps):
    return {"jobs": [{"steps": list(steps)}]}


def test_summer_utc_crons_select_two_athens_slots():
    assert scheduled_slot("17 6 * * *", _now("2026-08-28T06:20:00Z")).name == "primary"
    assert scheduled_slot("17 7 * * *", _now("2026-08-28T07:20:00Z")).name == "fallback"
    assert scheduled_slot("17 8 * * *", _now("2026-08-28T08:20:00Z")) is None


def test_winter_utc_crons_select_two_athens_slots():
    assert scheduled_slot("17 6 * * *", _now("2026-12-28T06:20:00Z")) is None
    assert scheduled_slot("17 7 * * *", _now("2026-12-28T07:20:00Z")).name == "primary"
    assert scheduled_slot("17 8 * * *", _now("2026-12-28T08:20:00Z")).name == "fallback"


def test_dst_transition_sundays_use_correct_offsets():
    assert scheduled_slot("17 6 * * *", _now("2026-03-29T06:20:00Z")).name == "primary"
    assert scheduled_slot("17 7 * * *", _now("2026-10-25T07:20:00Z")).name == "primary"


def test_delayed_slot_after_utc_midnight_keeps_intended_athens_date():
    slot = scheduled_slot("17 6 * * *", _now("2026-08-28T00:30:00Z"))

    assert slot.name == "primary"
    assert slot.local_date == date(2026, 8, 27)


def test_unknown_schedule_is_rejected():
    assert scheduled_slot("0 6 * * *", _now("2026-08-28T06:20:00Z")) is None


def test_manual_dispatch_bypasses_daily_gate():
    decision = decide(
        event_name="workflow_dispatch",
        event_schedule="",
        now=_now("2026-08-28T08:00:00Z"),
        repository=REPOSITORY,
        current_run_id=10,
        get_json=lambda _url: (_ for _ in ()).throw(AssertionError("unexpected API call")),
    )

    assert decision.run is True
    assert decision.slot == "manual"


def test_inactive_utc_candidate_skips_without_github_lookup():
    decision = decide(
        event_name="schedule",
        event_schedule="17 8 * * *",
        now=_now("2026-08-28T08:20:00Z"),
        repository=REPOSITORY,
        current_run_id=10,
        get_json=lambda _url: (_ for _ in ()).throw(AssertionError("unexpected API call")),
    )

    assert decision.run is False
    assert decision.slot == "inactive-schedule"


def test_started_failed_triage_suppresses_recovery_and_query_includes_all_events():
    def get_json(url):
        if "/actions/workflows/" in url:
            assert "event=" not in url
            return _runs({"id": 101, "created_at": "2026-08-28T06:20:00Z"})
        return _jobs(
            {
                "name": "Run triage",
                "started_at": "2026-08-28T06:21:00Z",
                "conclusion": "failure",
            }
        )

    decision = decide(
        event_name="workflow_dispatch",
        event_schedule="",
        now=_now("2026-08-28T08:00:00Z"),
        repository=REPOSITORY,
        current_run_id=102,
        get_json=get_json,
        daily_recovery=True,
    )

    assert decision.run is False
    assert decision.slot == "recovery"
    assert decision.prior_run_id == 101


def test_condition_skipped_triage_does_not_consume_day_even_with_timestamp():
    def get_json(url):
        if "/actions/workflows/" in url:
            return _runs({"id": 101, "created_at": "2026-08-28T06:20:00Z"})
        return _jobs(
            {
                "name": "Run triage",
                "started_at": "2026-08-28T06:21:00Z",
                "conclusion": "skipped",
            }
        )

    decision = decide(
        event_name="workflow_dispatch",
        event_schedule="",
        now=_now("2026-08-28T08:00:00Z"),
        repository=REPOSITORY,
        current_run_id=102,
        get_json=get_json,
        daily_recovery=True,
    )

    assert decision.run is True


def test_auth_failure_before_triage_allows_scheduled_fallback():
    def get_json(url):
        if "/actions/workflows/" in url:
            return _runs({"id": 101, "created_at": "2026-08-28T06:20:00Z"})
        return _jobs(
            {"name": "Validate Gmail auth", "started_at": "2026-08-28T06:21:00Z"},
            {"name": "Run triage", "started_at": None, "conclusion": "skipped"},
        )

    decision = decide(
        event_name="schedule",
        event_schedule="17 7 * * *",
        now=_now("2026-08-28T07:20:00Z"),
        repository=REPOSITORY,
        current_run_id=102,
        get_json=get_json,
    )

    assert decision.run is True
    assert decision.slot == "fallback"


def test_recovery_runs_when_no_prior_triage_exists():
    decision = decide(
        event_name="workflow_dispatch",
        event_schedule="",
        now=_now("2026-08-28T08:00:00Z"),
        repository=REPOSITORY,
        current_run_id=102,
        get_json=lambda _url: _runs(),
        daily_recovery=True,
    )

    assert decision.run is True
    assert decision.slot == "recovery"


def test_prior_run_outside_athens_date_and_current_run_are_ignored():
    def get_json(url):
        if "/actions/workflows/" in url:
            return _runs(
                {"id": 100, "created_at": "2026-08-27T06:20:00Z"},
                {"id": 102, "created_at": "2026-08-28T07:17:00Z"},
            )
        raise AssertionError("jobs lookup should not run")

    prior = find_prior_started_triage(
        repository=REPOSITORY,
        current_run_id=102,
        local_date=date(2026, 8, 28),
        get_json=get_json,
    )

    assert prior is None


def test_invalid_github_response_fails_closed():
    with pytest.raises(RuntimeError, match="workflow_runs"):
        decide(
            event_name="schedule",
            event_schedule="17 6 * * *",
            now=_now("2026-08-28T06:20:00Z"),
            repository=REPOSITORY,
            current_run_id=102,
            get_json=lambda _url: {},
        )


def test_workflow_uses_only_explicit_utc_candidates():
    workflow = Path(".github/workflows/gmail-triage.yml").read_text(encoding="utf-8")

    assert 'cron: "17 6 * * *"' in workflow
    assert 'cron: "17 7 * * *"' in workflow
    assert 'cron: "17 8 * * *"' in workflow
    assert workflow.count("cron:") == 3
    assert "timezone:" not in workflow
