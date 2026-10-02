"""Build the public, read-only GitHub Pages view from repository source files."""

from __future__ import annotations

import argparse
import ast
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "vpapats/gmail-cleanup-agent"


def _assignment(module: ast.Module, name: str) -> ast.AST:
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
    raise ValueError(f"Missing source constant: {name}")


def _time(module: ast.Module, name: str) -> str:
    value = _assignment(module, name)
    if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "time":
        raise ValueError(f"Unexpected {name} source format")
    hour, minute = (ast.literal_eval(arg) for arg in value.args[:2])
    return f"{hour:02d}:{minute:02d}"


def _workflow(path: str) -> dict:
    document = yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))
    return document


def _triggers(document: dict) -> dict:
    # PyYAML's YAML 1.1 resolver treats the workflow key 'on' as boolean True.
    return document.get("on", document.get(True, {}))


def _secret_name(expression: str) -> str | None:
    match = re.fullmatch(r"\$\{\{\s*secrets\.([A-Z0-9_]+)\s*\}\}", str(expression))
    return match.group(1) if match else None


def collect_data(sha: str) -> dict:
    settings = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
    daily = _workflow(".github/workflows/gmail-triage.yml")
    watchdog = _workflow(".github/workflows/gmail-triage-watchdog.yml")
    weekly = _workflow(".github/workflows/weekly-quality-audit.yml")
    daily_env = daily["jobs"]["run-triage"]["env"]
    weekly_env = weekly["jobs"]["weekly-audit"]["env"]
    daily_secrets = {
        key: name for key, value in daily_env.items() if (name := _secret_name(value))
    }
    weekly_secrets = {
        key: name for key, value in weekly_env.items() if (name := _secret_name(value))
    }
    scopes_module = ast.parse((ROOT / "src/auth.py").read_text(encoding="utf-8"))
    classifier_text = (ROOT / "src/classifier.py").read_text(encoding="utf-8")
    classifier = ast.parse(classifier_text)
    gate = ast.parse((ROOT / "scripts/daily_schedule_gate.py").read_text(encoding="utf-8"))
    protections = _assignment(classifier, "PROTECTION_PATTERNS")
    if not isinstance(protections, ast.Dict):
        raise ValueError("Unexpected protection rule format")
    protection_names = [ast.literal_eval(key) for key in protections.keys]
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    if endpoint not in classifier_text:
        raise ValueError("OpenRouter API endpoint was not found in the classifier")
    limit_setting = str(daily_env["OPENROUTER_MAX_ATTACHMENT_BYTES"])
    limit_match = re.search(r"[\'\"](\d+)[\'\"]", limit_setting)
    if not limit_match:
        raise ValueError("Attachment limit default was not found in the workflow")
    model = daily_env["OPENROUTER_MODEL"]
    if model != weekly_env["OPENROUTER_MODEL"]:
        raise ValueError("Daily and weekly workflows use different models")
    if settings["mode"] not in {"shadow", "active"}:
        raise ValueError("Unknown triage mode")
    if not 0 <= settings["min_trash_confidence"] <= 1:
        raise ValueError("Invalid confidence threshold")
    if not set(settings["daily_summary"]["decisions"]) <= set(settings["labels"]):
        raise ValueError("Summary decision is missing a Gmail label")
    return {
        "repository": REPOSITORY,
        "sha": sha,
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": settings,
        "model": model,
        "endpoint": endpoint,
        "attachment_limit_default": int(limit_match.group(1)),
        "oauth_scopes": ast.literal_eval(_assignment(scopes_module, "GMAIL_SCOPES")),
        "daily_secrets": daily_secrets,
        "weekly_secrets": weekly_secrets,
        "protection_signals": protection_names,
        "hard_protections": sorted(ast.literal_eval(_assignment(classifier, "HARD_PROTECTION_HITS"))),
        "low_value_patterns": ast.literal_eval(_assignment(classifier, "LOW_VALUE_PATTERNS")),
        "schedule": {
            "primary": _time(gate, "PRIMARY_TIME"),
            "fallback": _time(gate, "FALLBACK_TIME"),
            "daily_probes_utc": [entry["cron"] for entry in _triggers(daily)["schedule"]],
            "watchdog": [
                {"cron": entry["cron"], "timezone": entry.get("timezone", "UTC")}
                for entry in _triggers(watchdog)["schedule"]
            ],
            "weekly_utc": [entry["cron"] for entry in _triggers(weekly)["schedule"]],
        },
    }


def build(output: Path, sha: str) -> None:
    data = collect_data(sha)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = (ROOT / "dashboard/index.html").read_text(encoding="utf-8")
    if "__DASHBOARD_DATA__" not in template:
        raise ValueError("Dashboard template is missing its data marker")
    page = template.replace("__DASHBOARD_DATA__", payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "_site/index.html")
    parser.add_argument("--sha", default=os.getenv("GITHUB_SHA", "local-preview"))
    args = parser.parse_args()
    build(args.output, args.sha)
    print(f"Built {args.output}")


if __name__ == "__main__":
    main()
