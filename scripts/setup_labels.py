from __future__ import annotations

import argparse

import yaml

from src.gmail_client import GmailClient


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    client = GmailClient()
    for name in cfg["labels"].values():
        label_id = client.ensure_label(name)
        print(f"Ensured label: {name} ({label_id})")


if __name__ == "__main__":
    main()
