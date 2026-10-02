import json
import re

from scripts import build_dashboard


def test_dashboard_extracts_current_public_settings():
    data = build_dashboard.collect_data("test-sha")
    assert data["settings"]["labels"]["digest_and_trash"] == "AI/Digest-and-Trash"
    assert data["daily_secrets"]["OPENROUTER_API_KEY"] == "OPENROUTER_API_KEY_AETHERISPC"
    assert data["schedule"]["primary"] == "09:17"
    assert data["schedule"]["fallback"] == "10:17"
    assert data["attachment_limit_default"] == 750000
    assert not {"message_body", "refresh_token", "api_key"} & data.keys()


def test_embedded_data_cannot_close_json_script(tmp_path, monkeypatch):
    data = build_dashboard.collect_data("test-sha")
    attack = "</script><script>alert('x')</script>"
    data["settings"]["candidate_queries"] = [attack]
    monkeypatch.setattr(build_dashboard, "collect_data", lambda sha: data)
    output = tmp_path / "index.html"
    build_dashboard.build(output, "test-sha")
    page = output.read_text(encoding="utf-8")
    assert attack not in page
    payload = re.search(
        r'<script type="application/json" id="dashboard-data">(.*?)</script>',
        page,
        re.DOTALL,
    ).group(1)
    assert json.loads(payload)["settings"]["candidate_queries"] == [attack]
