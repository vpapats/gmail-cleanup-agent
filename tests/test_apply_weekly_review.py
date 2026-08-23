import base64
import json
from pathlib import Path

from scripts.apply_weekly_review import _load_remote_ledger


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
    assert "group: gmail-fomo-writes" in workflow
