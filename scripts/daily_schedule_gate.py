from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo


ATHENS = ZoneInfo("Europe/Athens")
WORKFLOW = "gmail-triage.yml"
SCHEDULED_SLOTS = {
    "17 9 * * *": ("primary", time(9, 17)),
    "17 10 * * *": ("fallback", time(10, 17)),
}


@dataclass(frozen=True)
class ScheduledSlot:
    name: str
    local_date: date


@dataclass(frozen=True)
class GateDecision:
    run: bool
    slot: str
    local_date: date
    reason: str
    prior_run_id: int | None = None


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("--now must include a timezone")
    return parsed.astimezone(timezone.utc)


def scheduled_slot(event_schedule: str, now: datetime) -> ScheduledSlot | None:
    configured_slot = SCHEDULED_SLOTS.get(event_schedule.strip())
    if configured_slot is None:
        return None
    name, scheduled_time = configured_slot
    local_now = now.astimezone(ATHENS)
    local_date = local_now.date()
    if local_now.time().replace(tzinfo=None) < scheduled_time:
        local_date -= timedelta(days=1)
    return ScheduledSlot(name, local_date)


def _parse_github_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def find_prior_started_triage(
    *,
    repository: str,
    current_run_id: int,
    local_date: date,
    get_json: Callable[[str], dict[str, Any]],
    workflow: str = WORKFLOW,
) -> int | None:
    start = datetime.combine(local_date, time.min, tzinfo=ATHENS).astimezone(timezone.utc)
    end = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=ATHENS).astimezone(
        timezone.utc
    )
    query = urllib.parse.urlencode({"event": "schedule", "per_page": 100})
    workflow_path = urllib.parse.quote(workflow, safe="")
    runs_url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/"
        f"{workflow_path}/runs?{query}"
    )
    runs = get_json(runs_url).get("workflow_runs")
    if not isinstance(runs, list):
        raise RuntimeError("GitHub workflow-runs response is missing workflow_runs")
    for run in runs:
        if not isinstance(run, dict):
            raise RuntimeError("GitHub workflow-runs response contains an invalid run")
        run_id = int(run.get("id", 0))
        created_at = str(run.get("created_at", ""))
        if not run_id or run_id == current_run_id or not created_at:
            continue
        created = _parse_github_timestamp(created_at)
        if not start <= created < end:
            continue
        jobs_url = (
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}/jobs"
            "?filter=all&per_page=100"
        )
        jobs = get_json(jobs_url).get("jobs")
        if not isinstance(jobs, list):
            raise RuntimeError("GitHub jobs response is missing jobs")
        for job in jobs:
            if not isinstance(job, dict):
                raise RuntimeError("GitHub jobs response contains an invalid job")
            steps = job.get("steps") or []
            if not isinstance(steps, list):
                raise RuntimeError("GitHub jobs response contains invalid steps")
            for step in steps:
                if not isinstance(step, dict):
                    raise RuntimeError("GitHub jobs response contains an invalid step")
                if step.get("name") == "Run triage" and step.get("started_at"):
                    return run_id
    return None


def decide(
    *,
    event_name: str,
    event_schedule: str,
    now: datetime,
    repository: str,
    current_run_id: int,
    get_json: Callable[[str], dict[str, Any]],
) -> GateDecision:
    local_date = now.astimezone(ATHENS).date()
    if event_name == "workflow_dispatch":
        return GateDecision(True, "manual", local_date, "Manual run requested.")
    if event_name != "schedule":
        return GateDecision(False, "unsupported", local_date, "Unsupported workflow event.")

    slot = scheduled_slot(event_schedule, now)
    if slot is None:
        return GateDecision(
            False,
            "unknown-schedule",
            local_date,
            "This scheduled expression is not an approved Athens daily slot.",
        )
    prior_run_id = find_prior_started_triage(
        repository=repository,
        current_run_id=current_run_id,
        local_date=slot.local_date,
        get_json=get_json,
    )
    if prior_run_id is not None:
        return GateDecision(
            False,
            slot.name,
            slot.local_date,
            "A prior scheduled run already started triage for this Athens date.",
            prior_run_id=prior_run_id,
        )
    return GateDecision(
        True,
        slot.name,
        slot.local_date,
        f"No prior scheduled run started triage before the {slot.name} Athens slot.",
    )


def github_get_json(token: str) -> Callable[[str], dict[str, Any]]:
    if not token:
        raise ValueError("GITHUB_TOKEN is required for scheduled gate checks")

    def get_json(url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "gmail-fomo-daily-schedule-gate",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise RuntimeError("GitHub returned an invalid schedule-gate response")
        return payload

    return get_json


def emit(decision: GateDecision, output_path: str, summary_path: str) -> None:
    values = {
        "run": str(decision.run).lower(),
        "slot": decision.slot,
        "local_date": decision.local_date.isoformat(),
        "prior_run_id": str(decision.prior_run_id or ""),
    }
    if output_path:
        with Path(output_path).open("a", encoding="utf-8") as output:
            for key, value in values.items():
                output.write(f"{key}={value}\n")
    if summary_path:
        action = "run" if decision.run else "skip"
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(
                f"- Daily schedule gate: **{action}** ({decision.slot}, "
                f"{decision.local_date.isoformat()}) — {decision.reason}\n"
            )
    print(json.dumps({**values, "reason": decision.reason}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-name", default=os.getenv("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-schedule", default=os.getenv("GITHUB_EVENT_SCHEDULE", ""))
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", type=int, default=int(os.getenv("GITHUB_RUN_ID", "0")))
    parser.add_argument("--token", default=os.getenv("GITHUB_TOKEN", ""))
    parser.add_argument("--now", help="UTC-aware ISO timestamp used by tests and diagnostics")
    args = parser.parse_args()

    now = parse_now(args.now)
    get_json: Callable[[str], dict[str, Any]]
    if args.event_name == "schedule":
        if not args.repository or not args.run_id:
            raise SystemExit("GITHUB_REPOSITORY and GITHUB_RUN_ID are required")
        get_json = github_get_json(args.token)
    else:
        get_json = lambda _url: {}
    decision = decide(
        event_name=args.event_name,
        event_schedule=args.event_schedule,
        now=now,
        repository=args.repository,
        current_run_id=args.run_id,
        get_json=get_json,
    )
    emit(decision, os.getenv("GITHUB_OUTPUT", ""), os.getenv("GITHUB_STEP_SUMMARY", ""))


if __name__ == "__main__":
    main()
