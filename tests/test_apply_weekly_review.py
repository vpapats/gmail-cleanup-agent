import base64
import json
from pathlib import Path

import pytest

from scripts.apply_weekly_review import (
    _load_remote_ledger,
    _load_remote_review_files,
    _parse_selections,
)


class _Response:
    status_code = 200

    def json(self):
        content = json.dumps(
            {
                "review_id": "weekly-2026-08-10-2026-08-17",
                "status": "complete",
            }
        ).encode()
        return {"content": base64.b64encode(content).decode()}


def test_completed_remote_ledger_is_detectable_before_historical_reapply(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(
        "scripts.apply_weekly_review.requests.get",
        lambda *args, **kwargs: _Response(),
    )

    ledger = _load_remote_ledger("weekly-2026-08-10-2026-08-17")

    assert ledger["status"] == "complete"


def test_workflow_dispatch_input_is_passed_through_environment_not_shell_expression():
    workflow = Path(".github/workflows/apply-weekly-review.yml").read_text()

    assert "REVIEW_ID_INPUT: ${{ inputs.review_id }}" in workflow
    assert 'review_id="$REVIEW_ID_INPUT"' in workflow
    assert 'review_id="${{ inputs.review_id }}"' not in workflow
    assert "SELECTIONS_JSON_INPUT: ${{ inputs.selections_json }}" in workflow
    assert '--selections-json "$SELECTIONS_JSON_INPUT"' in workflow
    assert "review_branch=gmail-fomo-review-${review_id:7:10}" in workflow
    assert "ref: main" in workflow
    assert "ref: ${{ steps.review.outputs.review_branch }}" not in workflow
    assert '--review-ref "${{ steps.review.outputs.review_branch }}"' in workflow
    assert "pull_request:" not in workflow
    assert "group: gmail-fomo-writes" in workflow


def test_selection_json_rejects_duplicate_item_ids():
    with pytest.raises(SystemExit, match="invalid"):
        _parse_selections('{"aaaaaaaaaaaaaaaa":"kept","aaaaaaaaaaaaaaaa":"action_needed"}')


def test_review_files_are_read_from_one_immutable_commit(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "token")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    calls = []

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    sha = "a" * 40

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params")))
        if "/git/ref/heads/" in url:
            return Response({"object": {"sha": sha}})
        return Response({"content": base64.b64encode(url.encode()).decode()})

    monkeypatch.setattr("scripts.apply_weekly_review.requests.get", fake_get)
    public, private = _load_remote_review_files(
        "weekly-2026-08-10-2026-08-17", "gmail-fomo-review-2026-08-10"
    )

    assert b"weekly_reviews/weekly-2026-08-10-2026-08-17.json" in public
    assert b".gmail-fomo/reviews/weekly-2026-08-10-2026-08-17.enc.json" in private
    assert calls[1][1] == {"ref": sha}
    assert calls[2][1] == {"ref": sha}


def test_apps_script_retry_waits_for_a_new_ledger_attempt():
    code = Path("apps_script/weekly_review_relay/Code.gs").read_text()
    status = Path("apps_script/weekly_review_relay/Status.html").read_text()

    assert "const dispatchedAtMs = Date.now();" in code
    assert 'ledger.status === "incomplete" && Number(newerThanMs || 0) > appliedAtMs' in code
    assert "getApplyStatus(reviewId, newerThanMs)" in status
