from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from src.triage import TriageConfig, TriageRunner


def load_config(path: str) -> TriageConfig:
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return TriageConfig(
        mode=raw.get("mode", "shadow"),
        use_model=bool(raw.get("use_model", False)),
        min_trash_confidence=float(raw.get("min_trash_confidence", 0.93)),
        max_messages_per_run=int(raw.get("max_messages_per_run", 50)),
        max_trash_per_run=int(raw.get("max_trash_per_run", 10)),
        max_trash_per_sender=int(raw.get("max_trash_per_sender", 3)),
        approved_trash_senders=set(raw.get("approved_trash_senders", [])),
        candidate_queries=list(raw.get("candidate_queries", [])),
        labels=dict(raw.get("labels", {})),
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
