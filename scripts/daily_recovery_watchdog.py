from __future__ import annotations

import argparse
import ast
import io
import json
import os
import re
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


ATHENS = ZoneInfo("Europe/Athens")
TRIAGE_WORKFLOW = "gmail-triage.yml"
WATCHDOG_SLOTS = {
    "35 10 * * *": time(10, 35),
    "35 14 * * *": time(14, 35),
    "35 18 * * *": time(18, 35),
}
API_VERSION = "2026-03-10"
RUN_COMPLETE_RE = re.compile(r"Run complete:\s*(\{[^\r\n]+\})")


class WatchdogError(RuntimeError):
    pass


class DispatchOutcomeUncertain(WatchdogError):
    pass


class TriageSkipped(WatchdogError):
    pass


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        previous = urllib.parse.urlsplit(req.full_url)
        following = urllib.parse.urlsplit(newurl)
        previous_origin = (previous.scheme, previous.hostname, previous.port)
        following_origin = (following.scheme, following.hostname, following.port)
        if previous_origin != following_origin:
            redirected.remove_header("Authorization")
        return redirected


@dataclass(frozen=True)
class WatchdogSlot:
    name: str
    local_date: date
    stale: bool = False


@dataclass(frozen=True)
class TriageCoverage:
    run_id: int
    html_url: str
    head_sha: str
    started_at: datetime
    conclusion: str


@dataclass(frozen=True)
class Verification:
    run_id: int
    html_url: str
    summary_sent: int
    errors: int
    artifact_size: int


@dataclass(frozen=True)
class RecoveryPlan:
    before_ids: frozenset[int]
    expected_sha: str


class GitHubApi:
    def __init__(self, token: str, repository: str) -> None:
        if not token:
            raise WatchdogError("GITHUB_TOKEN is required")
        if not repository or "/" not in repository:
            raise WatchdogError("GITHUB_REPOSITORY must be OWNER/REPO")
        self.token = token
        self.repository = repository
        self.opener = urllib.request.build_opener(SafeRedirectHandler())

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None):
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "gmail-fomo-daily-recovery-watchdog",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
        try:
            return self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as err:
            if method == "POST" and err.code >= 500:
                raise DispatchOutcomeUncertain(
                    f"Workflow dispatch returned HTTP {err.code}; outcome is uncertain"
                ) from err
            raise WatchdogError(
                f"GitHub API {method} {path} failed with HTTP {err.code}"
            ) from err

    def get_json(self, path: str) -> dict[str, Any]:
        try:
            with self._request("GET", path) as response:
                payload = json.load(response)
        except (OSError, TimeoutError, json.JSONDecodeError) as err:
            raise WatchdogError(f"GitHub API GET {path} failed") from err
        if not isinstance(payload, dict):
            raise WatchdogError(f"GitHub API GET {path} returned invalid JSON")
        return payload

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._request("POST", path, payload) as response:
                raw = response.read()
        except WatchdogError:
            raise
        except (OSError, TimeoutError) as err:
            raise DispatchOutcomeUncertain("Workflow dispatch outcome is uncertain") from err
        if not raw:
            raise DispatchOutcomeUncertain("Workflow dispatch returned no run details")
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as err:
            raise DispatchOutcomeUncertain("Workflow dispatch returned invalid run details") from err
        if not isinstance(result, dict):
            raise DispatchOutcomeUncertain("Workflow dispatch returned invalid run details")
        return result

    def get_bytes(self, path: str) -> bytes:
        try:
            with self._request("GET", path) as response:
                return response.read()
        except (OSError, TimeoutError) as err:
            raise WatchdogError(f"GitHub API download {path} failed") from err


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(timezone.utc)


def select_slot(event_name: str, event_schedule: str, now: datetime) -> WatchdogSlot:
    local_now = now.astimezone(ATHENS)
    if event_name == "workflow_dispatch":
        return WatchdogSlot("manual", local_now.date())
    if event_name != "schedule":
        raise WatchdogError(f"Unsupported watchdog event: {event_name}")
    scheduled_time = WATCHDOG_SLOTS.get(event_schedule.strip())
    if scheduled_time is None:
        raise WatchdogError(f"Unknown watchdog schedule: {event_schedule}")
    intended_date = local_now.date()
    if local_now.time().replace(tzinfo=None) < scheduled_time:
        intended_date -= timedelta(days=1)
    return WatchdogSlot(
        event_schedule.split()[1] + ":35",
        intended_date,
        stale=intended_date != local_now.date(),
    )


def _parse_timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError) as err:
        raise WatchdogError("GitHub returned an invalid timestamp") from err


def _workflow_runs(api: GitHubApi) -> list[dict[str, Any]]:
    workflow = urllib.parse.quote(TRIAGE_WORKFLOW, safe="")
    payload = api.get_json(
        f"/repos/{api.repository}/actions/workflows/{workflow}/runs?per_page=100"
    )
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise WatchdogError("GitHub workflow-runs response is invalid")
    return runs


def _jobs(
    api: GitHubApi,
    run_id: int,
    *,
    filter_mode: str = "all",
) -> list[dict[str, Any]]:
    if filter_mode not in {"all", "latest"}:
        raise WatchdogError(f"Unsupported jobs filter: {filter_mode}")
    payload = api.get_json(
        f"/repos/{api.repository}/actions/runs/{run_id}/jobs?filter={filter_mode}&per_page=100"
    )
    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise WatchdogError(f"GitHub jobs response is invalid for run {run_id}")
    return jobs


def find_started_triage(api: GitHubApi, local_date: date) -> TriageCoverage | None:
    candidates: list[TriageCoverage] = []
    for run in _workflow_runs(api):
        run_id = int(run.get("id") or 0)
        if not run_id:
            continue
        for job in _jobs(api, run_id):
            steps = job.get("steps") or []
            if not isinstance(steps, list):
                raise WatchdogError(f"GitHub steps response is invalid for run {run_id}")
            for step in steps:
                if not isinstance(step, dict) or step.get("name") != "Run triage":
                    continue
                started_raw = step.get("started_at")
                conclusion = str(step.get("conclusion") or "")
                if not started_raw or conclusion == "skipped":
                    continue
                started = _parse_timestamp(str(started_raw))
                if started.astimezone(ATHENS).date() != local_date:
                    continue
                candidates.append(
                    TriageCoverage(
                        run_id=run_id,
                        html_url=str(run.get("html_url") or ""),
                        head_sha=str(run.get("head_sha") or ""),
                        started_at=started,
                        conclusion=conclusion,
                    )
                )
    unique = {candidate.run_id: candidate for candidate in candidates}
    if len(unique) > 1:
        raise WatchdogError(
            f"Multiple Run triage steps started for {local_date.isoformat()}: "
            + ", ".join(str(run_id) for run_id in sorted(unique))
        )
    return next(iter(unique.values()), None)


def _find_step(jobs: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for job in jobs:
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("name") == name:
                return step
    return None


def _wait_for_completion(
    api: GitHubApi,
    run_id: int,
    *,
    timeout_seconds: int,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    deadline = time_module.monotonic() + timeout_seconds
    path = f"/repos/{api.repository}/actions/runs/{run_id}"
    while True:
        run = api.get_json(path)
        if run.get("status") == "completed":
            return run
        if time_module.monotonic() >= deadline:
            raise WatchdogError(f"Timed out waiting for workflow run {run_id}")
        sleep(15)


def _run_counters(log_zip: bytes) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(log_zip))
    except (OSError, zipfile.BadZipFile) as err:
        raise WatchdogError("Workflow log archive is invalid") from err
    matches: list[dict[str, Any]] = []
    with archive:
        for name in archive.namelist():
            if name.endswith("/"):
                continue
            text = archive.read(name).decode("utf-8", errors="replace")
            member_matches: list[dict[str, Any]] = []
            for raw in RUN_COMPLETE_RE.findall(text):
                try:
                    parsed = ast.literal_eval(raw)
                except (SyntaxError, ValueError) as err:
                    raise WatchdogError("Run complete counters are invalid") from err
                if isinstance(parsed, dict):
                    member_matches.append(parsed)
            if len(member_matches) > 1:
                raise WatchdogError(
                    f"Log member {name} contains multiple Run complete counter records"
                )
            matches.extend(member_matches)
    # GitHub's workflow-log ZIP can mirror the same step output in both the
    # combined job log and the per-step log. Treat equivalent counter
    # records as one observation, but still fail closed if no record exists or
    # if the archive contains conflicting counter values.
    unique_matches: list[dict[str, Any]] = []
    for match in matches:
        if match not in unique_matches:
            unique_matches.append(match)
    if len(unique_matches) != 1:
        raise WatchdogError("Expected exactly one unique Run complete counter record")
    return unique_matches[0]


def _verify_run_evidence(
    api: GitHubApi,
    coverage: TriageCoverage,
    *,
    local_date: date,
    expected_sha: str | None = None,
    timeout_seconds: int = 1800,
    sleep: Callable[[float], None] = time_module.sleep,
) -> Verification:
    run = _wait_for_completion(
        api,
        coverage.run_id,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
    )
    if expected_sha and run.get("head_sha") != expected_sha:
        raise WatchdogError("Recovery run did not checkout the pre-dispatch main SHA")
    jobs = _jobs(api, coverage.run_id, filter_mode="latest")
    checkout = _find_step(jobs, "Checkout")
    if not checkout or checkout.get("conclusion") != "success":
        raise WatchdogError("Checkout did not succeed")
    triage = _find_step(jobs, "Run triage")
    if not triage or triage.get("conclusion") == "skipped":
        failed_conclusions = {"failure", "cancelled", "timed_out", "action_required"}
        failed_step = next(
            (
                step
                for job in jobs
                for step in (job.get("steps") or [])
                if isinstance(step, dict)
                and step.get("conclusion") in failed_conclusions
            ),
            None,
        )
        if failed_step:
            raise WatchdogError(
                f"Step {failed_step.get('name') or 'unknown'} ended with "
                f"{failed_step.get('conclusion')}; Run triage did not start"
            )
        schedule_gate = _find_step(jobs, "Select Athens daily slot")
        setup_python = _find_step(jobs, "Set up Python")
        if (
            schedule_gate
            and schedule_gate.get("conclusion") == "success"
            and setup_python
            and setup_python.get("conclusion") == "skipped"
        ):
            raise TriageSkipped("Run triage was skipped by the daily deduplication gate")
        raise WatchdogError("Run triage was skipped without verifiable deduplication")
    started_at = triage.get("started_at")
    if not started_at or _parse_timestamp(str(started_at)).astimezone(ATHENS).date() != local_date:
        raise WatchdogError("Run triage did not start on the target Athens date")
    if triage.get("conclusion") != "success":
        raise WatchdogError(
            f"Run triage consumed the day but ended with {triage.get('conclusion') or 'unknown'}"
        )
    artifacts_payload = api.get_json(
        f"/repos/{api.repository}/actions/runs/{coverage.run_id}/artifacts?per_page=100"
    )
    artifacts = artifacts_payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise WatchdogError("GitHub artifacts response is invalid")
    expected_name = f"triage-audit-{coverage.run_id}"
    matching = [
        artifact
        for artifact in artifacts
        if isinstance(artifact, dict) and artifact.get("name") == expected_name
    ]
    if len(matching) != 1:
        raise WatchdogError(f"Expected exactly one {expected_name} artifact")
    artifact = matching[0]
    artifact_size = int(artifact.get("size_in_bytes") or 0)
    if artifact.get("expired") or artifact_size <= 0:
        raise WatchdogError("Triage audit artifact is expired or empty")
    counters = _run_counters(
        api.get_bytes(f"/repos/{api.repository}/actions/runs/{coverage.run_id}/logs")
    )
    summary_sent = int(counters.get("summary_sent", -1))
    errors = int(counters.get("errors", -1))
    if summary_sent != 1 or errors != 0:
        raise WatchdogError(
            f"Invalid triage counters: summary_sent={summary_sent}, errors={errors}"
        )
    return Verification(
        run_id=coverage.run_id,
        html_url=str(run.get("html_url") or coverage.html_url),
        summary_sent=summary_sent,
        errors=errors,
        artifact_size=artifact_size,
    )


def verify_run(
    api: GitHubApi,
    coverage: TriageCoverage,
    *,
    local_date: date,
    expected_sha: str | None = None,
    timeout_seconds: int = 1800,
    sleep: Callable[[float], None] = time_module.sleep,
) -> Verification:
    reference = f"Run {coverage.run_id} ({coverage.html_url or 'URL unavailable'})"
    try:
        return _verify_run_evidence(
            api,
            coverage,
            local_date=local_date,
            expected_sha=expected_sha,
            timeout_seconds=timeout_seconds,
            sleep=sleep,
        )
    except TriageSkipped as err:
        raise TriageSkipped(f"{reference}: {err}") from err
    except WatchdogError as err:
        raise WatchdogError(f"{reference}: {err}") from err


def _main_sha(api: GitHubApi) -> str:
    payload = api.get_json(f"/repos/{api.repository}/branches/main")
    commit = payload.get("commit")
    sha = commit.get("sha") if isinstance(commit, dict) else ""
    if not sha:
        raise WatchdogError("Could not resolve the current main SHA")
    return str(sha)


def prepare_recovery(api: GitHubApi) -> RecoveryPlan:
    return RecoveryPlan(
        before_ids=frozenset(
            int(run.get("id") or 0) for run in _workflow_runs(api)
        ),
        expected_sha=_main_sha(api),
    )


def _reconcile_uncertain_dispatch(
    api: GitHubApi,
    before_ids: set[int],
    *,
    expected_sha: str,
    sleep: Callable[[float], None],
) -> int:
    for _attempt in range(6):
        candidates = [
            int(run.get("id") or 0)
            for run in _workflow_runs(api)
            if int(run.get("id") or 0) not in before_ids
            and run.get("event") == "workflow_dispatch"
            and run.get("head_branch") == "main"
            and run.get("head_sha") == expected_sha
        ]
        candidates = [run_id for run_id in candidates if run_id]
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            raise DispatchOutcomeUncertain("Multiple recovery candidates followed dispatch")
        sleep(10)
    raise DispatchOutcomeUncertain("Dispatch outcome remained uncertain; not retrying")


def dispatch_recovery(
    api: GitHubApi,
    *,
    plan: RecoveryPlan | None = None,
    sleep: Callable[[float], None] = time_module.sleep,
) -> tuple[int, str]:
    plan = plan or prepare_recovery(api)
    workflow = urllib.parse.quote(TRIAGE_WORKFLOW, safe="")
    try:
        response = api.post_json(
            f"/repos/{api.repository}/actions/workflows/{workflow}/dispatches",
            {"ref": "main", "inputs": {"daily_recovery": True}},
        )
        run_id = int(response.get("workflow_run_id") or 0)
        if not run_id:
            run_id = _reconcile_uncertain_dispatch(
                api,
                set(plan.before_ids),
                expected_sha=plan.expected_sha,
                sleep=sleep,
            )
    except DispatchOutcomeUncertain:
        run_id = _reconcile_uncertain_dispatch(
            api,
            set(plan.before_ids),
            expected_sha=plan.expected_sha,
            sleep=sleep,
        )
    return run_id, plan.expected_sha


def _coverage_for_run(api: GitHubApi, run_id: int) -> TriageCoverage:
    fallback_url = f"https://github.com/{api.repository}/actions/runs/{run_id}"
    try:
        run = api.get_json(f"/repos/{api.repository}/actions/runs/{run_id}")
    except WatchdogError as err:
        raise WatchdogError(
            f"Run {run_id} ({fallback_url}): could not read dispatched run details"
        ) from err
    return TriageCoverage(
        run_id=run_id,
        html_url=str(run.get("html_url") or fallback_url),
        head_sha=str(run.get("head_sha") or ""),
        started_at=datetime.now(timezone.utc),
        conclusion=str(run.get("conclusion") or ""),
    )


def run_watchdog(
    api: GitHubApi,
    *,
    slot: WatchdogSlot,
    sleep: Callable[[float], None] = time_module.sleep,
) -> tuple[Verification | None, bool, str]:
    if slot.stale:
        return None, False, "stale-schedule"
    first = find_started_triage(api, slot.local_date)
    if first is not None:
        return verify_run(api, first, local_date=slot.local_date, sleep=sleep), False, "covered"
    plan = prepare_recovery(api)
    second = find_started_triage(api, slot.local_date)
    if second is not None:
        return verify_run(api, second, local_date=slot.local_date, sleep=sleep), False, "race-covered"
    run_id, expected_sha = dispatch_recovery(api, plan=plan, sleep=sleep)
    dispatched = _coverage_for_run(api, run_id)
    try:
        verification = verify_run(
            api,
            dispatched,
            local_date=slot.local_date,
            expected_sha=expected_sha,
            sleep=sleep,
        )
        return verification, True, "recovered"
    except TriageSkipped as skipped:
        racing = find_started_triage(api, slot.local_date)
        if racing is None or racing.run_id == run_id:
            raise WatchdogError(
                f"{skipped}; no racing Run triage step was found for "
                f"{slot.local_date.isoformat()}"
            )
        return verify_run(api, racing, local_date=slot.local_date, sleep=sleep), True, "race-deduped"


def emit(
    verification: Verification | None,
    *,
    slot: WatchdogSlot,
    dispatched: bool,
    outcome: str,
    output_path: str,
    summary_path: str,
) -> None:
    values = {
        "verify_gmail": str(verification is not None).lower(),
        "local_date": slot.local_date.isoformat(),
        "triage_run_id": str(verification.run_id if verification else ""),
        "triage_run_url": verification.html_url if verification else "",
        "recovery_dispatched": str(dispatched).lower(),
        "outcome": outcome,
    }
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            if verification:
                summary.write(
                    f"- Gmail triage coverage: **{outcome}** for {slot.local_date.isoformat()} "
                    f"([run {verification.run_id}]({verification.html_url})); "
                    f"summary_sent={verification.summary_sent}, errors={verification.errors}, "
                    f"artifact={verification.artifact_size} bytes.\n"
                )
            else:
                summary.write("- Stale delayed watchdog event skipped without dispatch.\n")
    print(json.dumps(values, ensure_ascii=False))


def emit_failure(error: Exception, *, slot: WatchdogSlot | None, summary_path: str) -> None:
    message = " ".join(str(error).splitlines())
    local_date = slot.local_date.isoformat() if slot else "unknown date"
    print(f"::error::{message}")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(
                f"- Gmail triage watchdog: **failed** for {local_date}: {message}\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", default=os.getenv("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-schedule", default=os.getenv("GITHUB_EVENT_SCHEDULE", ""))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""))
    parser.add_argument("--now", help="UTC-aware ISO timestamp used by tests and diagnostics")
    args = parser.parse_args()

    slot: WatchdogSlot | None = None
    try:
        slot = select_slot(args.event_name, args.event_schedule, parse_now(args.now))
        api = GitHubApi(args.token, args.repository)
        verification, dispatched, outcome = run_watchdog(api, slot=slot)
        emit(
            verification,
            slot=slot,
            dispatched=dispatched,
            outcome=outcome,
            output_path=os.getenv("GITHUB_OUTPUT", ""),
            summary_path=os.getenv("GITHUB_STEP_SUMMARY", ""),
        )
    except (WatchdogError, ValueError) as err:
        emit_failure(
            err,
            slot=slot,
            summary_path=os.getenv("GITHUB_STEP_SUMMARY", ""),
        )
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
