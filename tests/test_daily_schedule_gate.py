from datetime import date, datetime, timezone

import pytest

from scripts.daily_schedule_gate import decide, find_prior_started_triage, scheduled_slot


def _now(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_timezone_aware_crons_select_primary_and_fallback():
    primary = scheduled_slot("17 9 * * *", _now("2026-08-27T06:40:00Z"))
    fallback = scheduled_slot("17 10 * * *", _now("2026-08-27T07:40:00Z"))

    assert primary.name == "primary"
    assert primary.local_date == date(2026, 8, 27)
    assert fallback.name == "fallback"


def test_unknown_schedule_is_rejected():
    assert scheduled_slot("0 6 * * *", _now("2026-08-27T06:40:00Z")) is None


def test_delayed_slot_after_athens_midnight_keeps_intended_date():
    slot = scheduled_slot("17 10 * * *", _now("2026-08-27T22:30:00Z"))

    assert slot.local_date == date(2026, 8, 27)


def test_manual_dispatch_always_runs_without_github_lookup():
    decision = decide(
        event_name="workflow_dispatch",
        event_schedule="",
        now=_now("2026-08-27T09:00:00Z"),
        repository="vpapats/gmail-cleanup-agent",
        current_run_id=10,
        get_json=lambda _url: (_ for _ in ()).throw(AssertionError("unexpected API call")),
    )

    assert decision.run is True
    assert decision.slot == "manual"


def test_unknown_scheduled_slot_skips_without_github_lookup():
    decision = decide(
        event_name="schedule",
        event_schedule="17 8 * * *",
        now=_now("2026-08-27T08:40:00Z"),
        repository="vpapats/gmail-cleanup-agent",
        current_run_id=10,
        get_json=lambda _url: (_ for _ in ()).throw(AssertionError("unexpected API call")),
    )

    assert decision.run is False
    assert decision.slot == "unknown-schedule"


def test_started_triage_suppresses_later_scheduled_slot():
    def get_json(url):
        if "/actions/workflows/" in url:
            return {"workflow_runs": [{"id": 101, "created_at": "2026-08-27T06:20:00Z"}]}
        assert "/actions/runs/101/jobs" in url
        return {
            "jobs": [
                {
                    "steps": [
                        {
                            "name": "Run triage",
                            "started_at": "2026-08-27T06:21:00Z",
                            "conclusion": "failure",
                        }
                    ]
                }
            ]
        }

    decision = decide(
        event_name="schedule",
        event_schedule="17 10 * * *",
        now=_now("2026-08-27T07:20:00Z"),
        repository="vpapats/gmail-cleanup-agent",
        current_run_id=102,
        get_json=get_json,
    )

    assert decision.run is False
    assert decision.slot == "fallback"
    assert decision.prior_run_id == 101


def test_auth_failure_before_triage_allows_fallback():
    def get_json(url):
        if "/actions/workflows/" in url:
            return {"workflow_runs": [{"id": 101, "created_at": "2026-08-27T06:20:00Z"}]}
        return {
            "jobs": [
                {
                    "steps": [
                        {"name": "Validate Gmail auth", "started_at": "2026-08-27T06:21:00Z"},
                        {"name": "Run triage", "started_at": None, "conclusion": "skipped"},
                    ]
                }
            ]
        }

    decision = decide(
        event_name="schedule",
        event_schedule="17 10 * * *",
        now=_now("2026-08-27T07:20:00Z"),
        repository="vpapats/gmail-cleanup-agent",
        current_run_id=102,
        get_json=get_json,
    )

    assert decision.run is True


def test_prior_run_outside_athens_date_and_current_run_are_ignored():
    def get_json(url):
        if "/actions/workflows/" in url:
            return {
                "workflow_runs": [
                    {"id": 100, "created_at": "2026-08-26T06:20:00Z"},
                    {"id": 102, "created_at": "2026-08-27T07:17:00Z"},
                ]
            }
        raise AssertionError("jobs lookup should not run")

    prior = find_prior_started_triage(
        repository="vpapats/gmail-cleanup-agent",
        current_run_id=102,
        local_date=date(2026, 8, 27),
        get_json=get_json,
    )

    assert prior is None


def test_invalid_github_response_fails_closed():
    with pytest.raises(RuntimeError, match="workflow_runs"):
        decide(
            event_name="schedule",
            event_schedule="17 9 * * *",
            now=_now("2026-08-27T06:20:00Z"),
            repository="vpapats/gmail-cleanup-agent",
            current_run_id=102,
            get_json=lambda _url: {},
        )
