from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import yaml

from src.triage import DailySummaryConfig, TriageConfig, TriageRunner


def load_config(path: str) -> TriageConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    daily_summary_raw = raw.get("daily_summary", {})
    max_messages_per_run = int(raw.get("max_messages_per_run", 5000))
    return TriageConfig(
        mode=raw.get("mode", "shadow"),
        use_model=bool(raw.get("use_model", False)),
        min_trash_confidence=float(raw.get("min_trash_confidence", 0.85)),
        max_messages_per_run=max_messages_per_run,
        recent_messages_per_run=int(raw.get("recent_messages_per_run", min(20, max_messages_per_run))),
        candidate_scan_limit=int(raw.get("candidate_scan_limit", max_messages_per_run)),
        approved_trash_senders=set(raw.get("approved_trash_senders", [])),
        candidate_queries=list(raw.get("candidate_queries", [])),
        labels=dict(raw.get("labels", {})),
        daily_summary=DailySummaryConfig(
            enabled=bool(daily_summary_raw.get("enabled", False)),
            decisions=set(daily_summary_raw.get("decisions", ["digest_and_trash"])),
            trash_after_send=bool(daily_summary_raw.get("trash_after_send", False)),
            send_when_empty=bool(daily_summary_raw.get("send_when_empty", False)),
            subject_prefix=str(daily_summary_raw.get("subject_prefix", "Today's GMAIL FOMO summary")),
        ),
    )


def _gmail_date(value: str, argument_name: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as err:
        raise SystemExit(f"{argument_name} must use YYYY-MM-DD format") from err
    return f"{parsed.year}/{parsed.month}/{parsed.day}"


def apply_manual_date_scope(
    candidate_queries: list[str],
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[str]:
    filters: list[str] = []
    if date_from:
        filters.append(f"after:{_gmail_date(date_from, '--date-from')}")
    if date_to:
        filters.append(f"before:{_gmail_date(date_to, '--date-to')}")
    if not filters:
        return candidate_queries
    suffix = " ".join(filters)
    return [f"{query} {suffix}".strip() for query in candidate_queries]


def apply_recheck_kept_scope(
    candidate_queries: list[str],
    *,
    kept_label: str,
    enabled: bool = False,
) -> list[str]:
    if not enabled:
        return candidate_queries
    exclusion = f" -label:{kept_label}"
    return [query.replace(exclusion, "") for query in candidate_queries]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--audit-dir", default="audit")
    parser.add_argument("--date-from", help="Limit manual runs to messages after this YYYY-MM-DD date.")
    parser.add_argument("--date-to", help="Limit manual runs to messages before this YYYY-MM-DD date.")
    parser.add_argument("--max-messages", type=int, help="Override max messages processed in this run.")
    parser.add_argument("--recent-messages", type=int, help="Override recent messages kept at the front.")
    parser.add_argument("--scan-limit", type=int, help="Override candidate scan limit.")
    parser.add_argument(
        "--recheck-kept",
        action="store_true",
        help="Include already-kept messages in a manual corrective review.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    config.candidate_queries = apply_manual_date_scope(
        config.candidate_queries,
        date_from=args.date_from,
        date_to=args.date_to,
    )
    config.candidate_queries = apply_recheck_kept_scope(
        config.candidate_queries,
        kept_label=config.labels["kept"],
        enabled=args.recheck_kept,
    )
    if args.max_messages is not None:
        config.max_messages_per_run = args.max_messages
    if args.recent_messages is not None:
        config.recent_messages_per_run = args.recent_messages
    if args.scan_limit is not None:
        config.candidate_scan_limit = args.scan_limit
    runner = TriageRunner(config=config, audit_dir=Path(args.audit_dir))
    stats = runner.run()
    print("Run complete:", stats)


if __name__ == "__main__":
    main()
