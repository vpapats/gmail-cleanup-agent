from __future__ import annotations

import io
import urllib.error
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from scripts import daily_recovery_watchdog as watchdog


REPOSITORY = "vpapats/gmail-cleanup-agent"
TARGET_DATE = date(2026, 8, 29)


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _coverage(
    run_id: int = 42,
    *,
    sha: str = "main-sha",
    conclusion: str = "success",
) -> watchdog.TriageCoverage:
    return watchdog.TriageCoverage(
        run_id=run_id,
        html_url=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        head_sha=sha,
        started_at=_utc("2026-08-29T07:02:00Z"),
        conclusion=conclusion,
    )


def _verification(run_id: int = 42) -> watchdog.Verification:
    return watchdog.Verification(
        run_id=run_id,
        html_url=f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        summary_sent=1,
        errors=0,
        artifact_size=321,
    )


def _log_zip(*records: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, record in enumerate(records):
            archive.writestr(
                f"triage/step-{index}.txt",
                f"prefix\nRun complete: {record}\nsuffix\n",
            )
    return buffer.getvalue()


class FakeApi:
    repository = REPOSITORY

    def __init__(self, get_json, *, log_zip: bytes = b"", post_json=None):
        self._get_json = get_json
        self._log_zip = log_zip
        self._post_json = post_json
        self.get_calls: list[str] = []
        self.byte_calls: list[str] = []
        self.post_calls: list[tuple[str, dict]] = []

    def get_json(self, path: str):
        self.get_calls.append(path)
        return self._get_json(path)

    def get_bytes(self, path: str) -> bytes:
        self.byte_calls.append(path)
        return self._log_zip

    def post_json(self, path: str, payload: dict):
        self.post_calls.append((path, payload))
        if self._post_json is None:
            raise AssertionError("unexpected dispatch")
        return self._post_json(path, payload)


def _run(run_id: int, *, created: str = "2026-08-29T07:00:00Z") -> dict:
    return {
        "id": run_id,
        "created_at": created,
        "head_sha": "main-sha",
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
    }


def _jobs(*steps: dict) -> dict:
    return {"jobs": [{"steps": list(steps)}]}


def _step(
    *,
    name: str = "Run triage",
    started_at: str | None = "2026-08-29T07:02:00Z",
    conclusion: str | None = "success",
) -> dict:
    return {"name": name, "started_at": started_at, "conclusion": conclusion}


def test_manual_slot_uses_current_athens_date():
    slot = watchdog.select_slot(
        "workflow_dispatch",
        "",
        _utc("2026-08-28T22:30:00Z"),
    )

    assert slot == watchdog.WatchdogSlot("manual", date(2026, 8, 29))


def test_delayed_schedule_from_prior_athens_day_is_stale():
    slot = watchdog.select_slot(
        "schedule",
        "35 10 * * *",
        _utc("2026-08-29T03:00:00Z"),
    )

    assert slot.local_date == date(2026, 8, 28)
    assert slot.stale is True


def test_find_started_triage_requires_exact_step_started_at_on_athens_date():
    runs = {
        "workflow_runs": [
            _run(1),
            _run(2),
            _run(3, created="2026-08-28T20:30:00Z"),
        ]
    }

    def get_json(path: str):
        if "/workflows/" in path:
            return runs
        if "/runs/1/jobs" in path:
            return _jobs(_step(started_at=None, conclusion=None))
        if "/runs/2/jobs" in path:
            return _jobs(_step(started_at="2026-08-29T07:03:00Z", conclusion="skipped"))
        if "/runs/3/jobs" in path:
            # 20:30 UTC on Aug 28 is 23:30 Athens on Aug 28, not target coverage.
            return _jobs(_step(started_at="2026-08-28T20:31:00Z"))
        raise AssertionError(path)

    assert watchdog.find_started_triage(FakeApi(get_json), TARGET_DATE) is None


def test_failed_started_triage_counts_as_coverage_and_is_not_redispatched():
    def get_json(path: str):
        if "/workflows/" in path:
            return {"workflow_runs": [_run(88)]}
        if "/runs/88/jobs" in path:
            return _jobs(_step(conclusion="failure"))
        raise AssertionError(path)

    result = watchdog.find_started_triage(FakeApi(get_json), TARGET_DATE)

    assert result is not None
    assert result.run_id == 88
    assert result.conclusion == "failure"


def test_multiple_runs_that_really_started_triage_fail_closed():
    def get_json(path: str):
        if "/workflows/" in path:
            return {"workflow_runs": [_run(1), _run(2)]}
        if "/jobs" in path:
            return _jobs(_step())
        raise AssertionError(path)

    with pytest.raises(watchdog.WatchdogError, match="Multiple Run triage steps"):
        watchdog.find_started_triage(FakeApi(get_json), TARGET_DATE)


def test_watchdog_performs_immediate_second_read_before_dispatch(monkeypatch):
    observations: list[str] = []
    covered = _coverage(77)
    verified = _verification(77)

    def find_started(_api, _local_date):
        observations.append("read")
        return None if observations == ["read"] else covered

    def verify(_api, coverage, **_kwargs):
        observations.append("verify")
        assert coverage == covered
        return verified

    monkeypatch.setattr(watchdog, "find_started_triage", find_started)
    monkeypatch.setattr(
        watchdog,
        "prepare_recovery",
        lambda _api: observations.append("prepare")
        or watchdog.RecoveryPlan(frozenset({1}), "main-sha"),
    )
    monkeypatch.setattr(watchdog, "verify_run", verify)
    monkeypatch.setattr(
        watchdog,
        "dispatch_recovery",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )

    result, dispatched, outcome = watchdog.run_watchdog(
        object(),
        slot=watchdog.WatchdogSlot("manual", TARGET_DATE),
    )

    assert observations == ["read", "prepare", "read", "verify"]
    assert result == verified
    assert dispatched is False
    assert outcome == "race-covered"


def test_recovery_plan_is_prepared_before_final_read_and_post(monkeypatch):
    observations: list[str] = []
    plan = watchdog.RecoveryPlan(frozenset({10}), "resolved-main")
    verified = _verification(99)

    def find_started(_api, _local_date):
        observations.append("read")
        return None

    def prepare(_api):
        observations.append("prepare")
        return plan

    def dispatch(_api, *, plan: watchdog.RecoveryPlan, sleep):
        observations.append("post")
        assert plan == watchdog.RecoveryPlan(frozenset({10}), "resolved-main")
        return 99, "resolved-main"

    def coverage_for_run(_api, run_id):
        observations.append("coverage")
        assert run_id == 99
        return _coverage(99, sha="resolved-main")

    def verify(_api, coverage, **kwargs):
        observations.append("verify")
        assert coverage.run_id == 99
        assert kwargs["expected_sha"] == "resolved-main"
        return verified

    monkeypatch.setattr(watchdog, "find_started_triage", find_started)
    monkeypatch.setattr(watchdog, "prepare_recovery", prepare)
    monkeypatch.setattr(watchdog, "dispatch_recovery", dispatch)
    monkeypatch.setattr(watchdog, "_coverage_for_run", coverage_for_run)
    monkeypatch.setattr(watchdog, "verify_run", verify)

    result, dispatched, outcome = watchdog.run_watchdog(
        object(),
        slot=watchdog.WatchdogSlot("manual", TARGET_DATE),
        sleep=lambda _seconds: None,
    )

    assert observations == ["read", "prepare", "read", "post", "coverage", "verify"]
    assert result == verified
    assert dispatched is True
    assert outcome == "recovered"


def test_dispatch_uses_main_recovery_input_and_returned_run_id():
    def get_json(path: str):
        if "/workflows/" in path:
            return {"workflow_runs": [_run(10)]}
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "resolved-main"}}
        raise AssertionError(path)

    api = FakeApi(
        get_json,
        post_json=lambda _path, _payload: {"workflow_run_id": 99},
    )

    run_id, expected_sha = watchdog.dispatch_recovery(api, sleep=lambda _seconds: None)

    assert run_id == 99
    assert expected_sha == "resolved-main"
    assert len(api.post_calls) == 1
    path, payload = api.post_calls[0]
    assert path.endswith("/workflows/gmail-triage.yml/dispatches")
    assert payload["ref"] == "main"
    assert payload["inputs"]["daily_recovery"] is True


def test_post_http_5xx_is_treated_as_uncertain_not_as_safe_to_retry(monkeypatch):
    api = watchdog.GitHubApi("test-token", REPOSITORY)
    error = urllib.error.HTTPError(
        "https://api.github.com/dispatch",
        502,
        "Bad Gateway",
        {},
        io.BytesIO(b""),
    )

    def fail_after_post(_request, timeout):
        assert timeout == 30
        raise error

    monkeypatch.setattr(api.opener, "open", fail_after_post)

    with pytest.raises(watchdog.DispatchOutcomeUncertain, match="HTTP 502"):
        api.post_json("/dispatch", {"ref": "main"})


def test_safe_redirect_strips_authorization_only_when_origin_changes():
    handler = watchdog.SafeRedirectHandler()
    request = watchdog.urllib.request.Request(
        "https://api.github.com/repos/example/actions/runs/1/logs",
        headers={"Authorization": "Bearer secret"},
    )

    cross_origin = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://objects.githubusercontent.com/artifact.zip",
    )
    same_origin = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://api.github.com/redirected/logs",
    )

    assert cross_origin is not None
    assert cross_origin.get_header("Authorization") is None
    assert same_origin is not None
    assert same_origin.get_header("Authorization") == "Bearer secret"


def test_uncertain_dispatch_is_reconciled_without_a_second_post():
    workflow_reads = 0

    def get_json(path: str):
        nonlocal workflow_reads
        if "/workflows/" in path:
            workflow_reads += 1
            if workflow_reads == 1:
                return {"workflow_runs": [_run(10)]}
            return {
                "workflow_runs": [
                    {
                        **_run(55),
                        "event": "workflow_dispatch",
                        "head_branch": "main",
                        "head_sha": "resolved-main",
                    },
                    _run(10),
                ]
            }
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "resolved-main"}}
        raise AssertionError(path)

    def uncertain(_path: str, _payload: dict):
        raise watchdog.DispatchOutcomeUncertain("connection ended after POST")

    api = FakeApi(get_json, post_json=uncertain)

    run_id, expected_sha = watchdog.dispatch_recovery(api, sleep=lambda _seconds: None)

    assert (run_id, expected_sha) == (55, "resolved-main")
    assert len(api.post_calls) == 1


def test_uncertain_dispatch_never_retries_when_no_run_can_be_reconciled():
    def get_json(path: str):
        if "/workflows/" in path:
            return {"workflow_runs": [_run(10)]}
        if path.endswith("/branches/main"):
            return {"commit": {"sha": "resolved-main"}}
        raise AssertionError(path)

    api = FakeApi(
        get_json,
        post_json=lambda _path, _payload: (_ for _ in ()).throw(
            watchdog.DispatchOutcomeUncertain("timeout")
        ),
    )

    with pytest.raises(watchdog.DispatchOutcomeUncertain, match="not retrying"):
        watchdog.dispatch_recovery(api, sleep=lambda _seconds: None)
    assert len(api.post_calls) == 1


def _verification_api(
    *,
    run_sha: str = "main-sha",
    checkout: str = "success",
    triage: str = "success",
    triage_started_at: str | None = "2026-08-29T07:02:00Z",
    artifacts: list[dict] | None = None,
    counters: str = "{'summary_sent': 1, 'errors': 0}",
) -> FakeApi:
    artifacts = (
        [{"name": "triage-audit-42", "size_in_bytes": 321, "expired": False}]
        if artifacts is None
        else artifacts
    )

    def get_json(path: str):
        if path.endswith("/actions/runs/42"):
            return {
                "id": 42,
                "status": "completed",
                "head_sha": run_sha,
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/42",
            }
        if path.endswith("/actions/runs/42/jobs?filter=latest&per_page=100"):
            return _jobs(
                _step(name="Checkout", conclusion=checkout),
                _step(started_at=triage_started_at, conclusion=triage),
            )
        if path.endswith("/actions/runs/42/artifacts?per_page=100"):
            return {"artifacts": artifacts}
        raise AssertionError(path)

    return FakeApi(get_json, log_zip=_log_zip(counters))


def test_verify_run_requires_sha_steps_nonempty_artifact_and_exact_counters():
    api = _verification_api()

    result = watchdog.verify_run(
        api,
        _coverage(),
        local_date=TARGET_DATE,
        expected_sha="main-sha",
        sleep=lambda _seconds: None,
    )

    assert result == _verification()
    assert any("jobs?filter=latest" in path for path in api.get_calls)
    assert not any("jobs?filter=all" in path for path in api.get_calls)
    assert api.byte_calls == [f"/repos/{REPOSITORY}/actions/runs/42/logs"]


def test_skipped_triage_is_dedup_only_when_gate_evidence_matches():
    def get_json(path: str):
        if path.endswith("/actions/runs/42"):
            return {
                "status": "completed",
                "head_sha": "main-sha",
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/42",
            }
        if path.endswith("/actions/runs/42/jobs?filter=latest&per_page=100"):
            return _jobs(
                _step(name="Checkout", conclusion="success"),
                _step(name="Select Athens daily slot", conclusion="success"),
                _step(name="Set up Python", conclusion="skipped"),
                _step(conclusion="skipped"),
            )
        raise AssertionError(path)

    with pytest.raises(
        watchdog.TriageSkipped,
        match=r"Run 42 \(https://github.com/.+\).*daily deduplication gate",
    ):
        watchdog.verify_run(
            FakeApi(get_json),
            _coverage(),
            local_date=TARGET_DATE,
            expected_sha="main-sha",
            sleep=lambda _seconds: None,
        )


def test_upstream_failure_is_not_misreported_as_dedup_and_includes_run_url():
    def get_json(path: str):
        if path.endswith("/actions/runs/42"):
            return {
                "status": "completed",
                "head_sha": "main-sha",
                "html_url": f"https://github.com/{REPOSITORY}/actions/runs/42",
            }
        if path.endswith("/actions/runs/42/jobs?filter=latest&per_page=100"):
            return _jobs(
                _step(name="Checkout", conclusion="success"),
                _step(name="Select Athens daily slot", conclusion="success"),
                _step(name="Set up Python", conclusion="success"),
                _step(name="Install dependencies", conclusion="failure"),
                _step(conclusion="skipped"),
            )
        raise AssertionError(path)

    with pytest.raises(
        watchdog.WatchdogError,
        match=r"Run 42 \(https://github.com/.+\).*Install dependencies ended with failure",
    ) as error:
        watchdog.verify_run(
            FakeApi(get_json),
            _coverage(),
            local_date=TARGET_DATE,
            expected_sha="main-sha",
            sleep=lambda _seconds: None,
        )

    assert not isinstance(error.value, watchdog.TriageSkipped)


@pytest.mark.parametrize(
    ("api", "expected_error"),
    [
        (_verification_api(run_sha="other-sha"), "pre-dispatch main SHA"),
        (_verification_api(checkout="failure"), "Checkout did not succeed"),
        (_verification_api(triage="failure"), "ended with failure"),
        (
            _verification_api(
                artifacts=[
                    {"name": "triage-audit-42", "size_in_bytes": 0, "expired": False}
                ]
            ),
            "expired or empty",
        ),
        (
            _verification_api(counters="{'summary_sent': 0, 'errors': 0}"),
            "summary_sent=0, errors=0",
        ),
        (
            _verification_api(counters="{'summary_sent': 1, 'errors': 1}"),
            "summary_sent=1, errors=1",
        ),
    ],
)
def test_verify_run_fails_closed_when_any_required_evidence_is_wrong(api, expected_error):
    with pytest.raises(watchdog.WatchdogError, match=expected_error):
        watchdog.verify_run(
            api,
            _coverage(),
            local_date=TARGET_DATE,
            expected_sha="main-sha",
            sleep=lambda _seconds: None,
        )


def test_run_counter_parser_accepts_mirrored_records_but_rejects_conflicts():
    with pytest.raises(watchdog.WatchdogError, match="exactly one"):
        watchdog._run_counters(_log_zip())

    expected = {"summary_sent": 1, "errors": 0}
    assert watchdog._run_counters(
        _log_zip(
            "{'summary_sent': 1, 'errors': 0}",
            "{'summary_sent': 1, 'errors': 0}",
        )
    ) == expected

    with pytest.raises(watchdog.WatchdogError, match="exactly one"):
        watchdog._run_counters(
            _log_zip(
                "{'summary_sent': 1, 'errors': 0}",
                "{'summary_sent': 0, 'errors': 0}",
            )
        )


def test_stale_watchdog_event_skips_all_github_activity():
    api = FakeApi(lambda path: (_ for _ in ()).throw(AssertionError(path)))

    result = watchdog.run_watchdog(
        api,
        slot=watchdog.WatchdogSlot("10:35", TARGET_DATE, stale=True),
    )

    assert result == (None, False, "stale-schedule")
    assert api.get_calls == []


def test_workflow_is_github_hosted_with_three_athens_slots_and_no_local_dependency():
    workflow = Path(".github/workflows/gmail-triage-watchdog.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.count("cron:") == 3
    assert 'cron: "35 10 * * *"' in workflow
    assert 'cron: "35 14 * * *"' in workflow
    assert 'cron: "35 18 * * *"' in workflow
    assert workflow.count('timezone: "Europe/Athens"') == 3
    assert "runs-on: ubuntu-latest" in workflow
    assert "actions: write" in workflow
    assert "contents: read" in workflow
    assert "${{ github.token }}" in workflow
    lowered = workflow.lower()
    assert "self-hosted" not in lowered
    assert "macos" not in lowered
    assert "launchd" not in lowered
    assert "codex" not in lowered
