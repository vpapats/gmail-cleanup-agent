from pathlib import Path


RELAY = Path("apps_script/weekly_review_relay/Code.gs")


def test_relay_verifies_signature_and_dispatches_only_the_apply_workflow():
    code = RELAY.read_text(encoding="utf-8")

    assert "computeHmacSha256Signature" in code
    assert "constantTimeEqual_" in code
    assert "apply-weekly-review.yml/dispatches" in code
    assert 'new Set(["kept", "action_needed", "digest_and_trash"])' in code
    assert "submittedChoiceKeys.length !== itemIds.length" in code


def test_relay_does_not_hold_or_request_gmail_credentials():
    code = RELAY.read_text(encoding="utf-8")
    manifest = Path(
        "apps_script/weekly_review_relay/appsscript.json"
    ).read_text(encoding="utf-8")

    assert "GmailApp" not in code
    assert "GOOGLE_REFRESH_TOKEN" not in code
    assert "mail.google.com/" not in manifest
