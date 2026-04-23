from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Review would-be trash messages from audit log")
    parser.add_argument("--audit-csv", default="audit/audit.csv")
    args = parser.parse_args()

    path = Path(args.audit_csv)
    if not path.exists():
        raise SystemExit(f"Audit CSV not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    candidates = [
        r
        for r in rows
        if r.get("decision") == "summarize_then_trash" and r.get("action_taken") != "trashed"
    ]

    print(f"Found {len(candidates)} shadow trash candidates")
    for row in candidates[:100]:
        print(f"- {row['sender']} | {row['subject']} | conf={row['confidence']} | {row['summary']}")


if __name__ == "__main__":
    main()
