import pytest

from scripts.gmail_oauth_bootstrap import _extract_code


def test_extract_code_from_plain_code():
    assert _extract_code("abc123") == "abc123"


def test_extract_code_from_redirect_url():
    url = "http://127.0.0.1:8765/callback?code=xyz789&scope=a"
    assert _extract_code(url) == "xyz789"


def test_extract_code_raises_without_code_param():
    with pytest.raises(ValueError):
        _extract_code("http://127.0.0.1:8765/callback?state=only")
