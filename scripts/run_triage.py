from __future__ import annotations

import argparse
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
        min_trash_confidence=float(raw.get("min_trash_confidence", 0.93)),
        max_messages_per_run=max_messages_per_run,
        recent_messages_per_run=int(raw.get("recent_messages_per_run", min(20, max_messages_per_run))),
        candidate_scan_limit=int(raw.get("candidate_scan_limit", max_messages_per_run)),
        approved_trash_senders=set(raw.get("approved_trash_senders", [])),
        candidate_queries=list(raw.get("candidate_queries", [])),
        labels=dict(raw.get("labels", {})),
        daily_summary=DailySummaryConfig(
            enabled=bool(daily_summary_raw.get("enabled", False)),
            decisions=set(daily_summary_raw.get("decisions", ["review", "low_priority"])),
            trash_after_send=bool(daily_summary_raw.get("trash_after_send", False)),
            send_when_empty=bool(daily_summary_raw.get("send_when_empty", False)),
            subject_prefix=str(daily_summary_raw.get("subject_prefix", "Today's GMAIL FOMO summary")),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--audit-dir", default="audit")
    args = parser.parse_args()

    config = load_config(args.config)
    runner = TriageRunner(config=config, audit_dir=Path(args.audit_dir))
    stats = runner.run()
    print("Run complete:", stats)


if __name__ == "__main__":
    main()
