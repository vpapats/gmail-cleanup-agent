from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

import yaml
import requests

from src.gmail_client import GmailClient
from src.github_state import GitHubFeedbackStateStore
from src.weekly_review import (
    REVIEW_ID_RE,
    apply_review_manifest,
    apply_review_selections,
    decrypt_private_items,
    load_review_manifest_bytes,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--selections-json", required=True)
    parser.add_argument("--review-ref", required=True)
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--ledger-dir", default="weekly_reviews/applied")
    args = parser.parse_args()

    if not REVIEW_ID_RE.fullmatch(args.review_id):
        raise SystemExit("Invalid review ID")
    expected_ref = f"gmail-fomo-review-{args.review_id[7:17]}"
    if args.review_ref != expected_ref:
        raise SystemExit("Review ref does not match review ID")

    previous = _load_remote_ledger(args.review_id)
    if previous is not None and previous.get("status") == "complete":
        print(json.dumps({"already_complete": True, "review_id": args.review_id}))
        return

    public_content, private_content = _load_remote_review_files(
        args.review_id, args.review_ref
    )
    manifest = load_review_manifest_bytes(public_content)
    if manifest.review_id != args.review_id:
        raise SystemExit("Review ID does not match manifest")
    manifest = apply_review_selections(
        manifest, _parse_selections(args.selections_json)
    )
    key = os.getenv("GMAIL_FOMO_STATE_KEY", "")
    private_items = decrypt_private_items(private_content, args.review_id, key)
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    labels = config.get("labels", {})
    label_names = {
        name: labels[name]
        for name in ("kept", "action_needed", "digest_and_trash")
    }

    ledger = apply_review_manifest(
        manifest=manifest,
        private_items=private_items,
        gmail=GmailClient(),
        feedback_state=GitHubFeedbackStateStore.from_env(),
        label_names=label_names,
    )
    ledger_dir = Path(args.ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / f"{args.review_id}.json"
    ledger_path.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _publish_ledger(args.review_id, ledger_path.read_bytes())
    print(json.dumps(ledger["counts"], sort_keys=True))
    if ledger["status"] != "complete":
        raise SystemExit("Weekly review apply is incomplete; inspect the committed ledger")


def _parse_selections(raw: str) -> dict[str, str]:
    def no_duplicate_keys(pairs: list[tuple[str, str]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate weekly review item ID")
            result[key] = value
        return result

    try:
        parsed = json.loads(raw, object_pairs_hook=no_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as err:
        raise SystemExit("Weekly review selections JSON is invalid") from err
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in parsed.items()
    ):
        raise SystemExit("Weekly review selections JSON is invalid")
    return parsed


def _load_remote_review_files(review_id: str, review_ref: str) -> tuple[bytes, bytes]:
    """Read only review data from an immutable review-branch commit.

    The workflow executes this script and all dependencies from trusted main. It
    never checks out or executes code from the historical review branch.
    """
    token = os.environ["GITHUB_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    ref_url = (
        f"https://api.github.com/repos/{repository}/git/ref/heads/{review_ref}"
    )
    ref_response = requests.get(ref_url, headers=headers, timeout=30)
    if ref_response.status_code != 200:
        raise RuntimeError(
            f"Weekly review ref lookup failed with HTTP {ref_response.status_code}"
        )
    try:
        commit_sha = ref_response.json()["object"]["sha"]
    except (KeyError, TypeError, ValueError) as err:
        raise RuntimeError("Weekly review ref response is invalid") from err
    if not isinstance(commit_sha, str) or len(commit_sha) != 40:
        raise RuntimeError("Weekly review ref response is invalid")

    paths = (
        f"weekly_reviews/{review_id}.json",
        f".gmail-fomo/reviews/{review_id}.enc.json",
    )
    contents: list[bytes] = []
    for path in paths:
        url = f"https://api.github.com/repos/{repository}/contents/{path}"
        response = requests.get(
            url, headers=headers, params={"ref": commit_sha}, timeout=30
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Weekly review file lookup failed with HTTP {response.status_code}"
            )
        try:
            contents.append(
                base64.b64decode(response.json()["content"].replace("\n", ""))
            )
        except (KeyError, TypeError, ValueError) as err:
            raise RuntimeError("Weekly review file response is invalid") from err
    return contents[0], contents[1]


def _publish_ledger(review_id: str, content: bytes) -> None:
    url, branch, headers = _ledger_request_details(review_id)
    existing = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
    body = {
        "message": f"Record {review_id} apply result",
        "content": base64.b64encode(content).decode(),
        "branch": branch,
    }
    if existing.status_code == 200:
        body["sha"] = existing.json()["sha"]
    elif existing.status_code != 404:
        raise RuntimeError(f"Weekly review ledger lookup failed with HTTP {existing.status_code}")
    written = requests.put(url, headers=headers, json=body, timeout=30)
    if written.status_code not in {200, 201}:
        raise RuntimeError(f"Weekly review ledger write failed with HTTP {written.status_code}")
    verified = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
    if verified.status_code != 200:
        raise RuntimeError("Weekly review ledger read-back failed")
    remote = base64.b64decode(verified.json()["content"].replace("\n", ""))
    if remote != content:
        raise RuntimeError("Weekly review ledger read-back did not match")


def _load_remote_ledger(review_id: str) -> dict | None:
    url, branch, headers = _ledger_request_details(review_id)
    response = requests.get(url, headers=headers, params={"ref": branch}, timeout=30)
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise RuntimeError(
            f"Weekly review ledger lookup failed with HTTP {response.status_code}"
        )
    try:
        content = base64.b64decode(response.json()["content"].replace("\n", ""))
        ledger = json.loads(content.decode("utf-8"))
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as err:
        raise RuntimeError("Weekly review ledger is invalid") from err
    if not isinstance(ledger, dict) or ledger.get("review_id") != review_id:
        raise RuntimeError("Weekly review ledger does not match the requested review")
    return ledger


def _ledger_request_details(review_id: str) -> tuple[str, str, dict[str, str]]:
    token = os.environ["GITHUB_TOKEN"]
    repository = os.environ["GITHUB_REPOSITORY"]
    branch = os.getenv("GMAIL_FOMO_STATE_BRANCH", "gmail-fomo-state")
    path = f".gmail-fomo/weekly-review-ledgers/{review_id}.json"
    url = f"https://api.github.com/repos/{repository}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return url, branch, headers


if __name__ == "__main__":
    main()
