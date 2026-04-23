from __future__ import annotations

import csv
import json
from pathlib import Path

from src.models import AuditRecord


class AuditLogger:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.out_dir / "audit.jsonl"
        self.csv_path = self.out_dir / "audit.csv"
        self._ensure_csv_header()

    def _ensure_csv_header(self) -> None:
        if self.csv_path.exists():
            return
        with self.csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(AuditRecord.__annotations__.keys()))
            writer.writeheader()

    def log(self, record: AuditRecord) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record.as_dict(), ensure_ascii=False) + "\n")
        with self.csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(AuditRecord.__annotations__.keys()))
            writer.writerow(record.as_dict())
