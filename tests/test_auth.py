import base64
import json
from io import BytesIO

import pytest

from codex_images_mcp._auth import OAuthResponsesError, oauth_headers


def test_loads_oauth_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({"tokens": {"access_token": "access", "account_id": "account"}}))
    headers = oauth_headers()
    assert headers["Authorization"] == "Bearer access"
    assert headers["chatgpt-account-id"] == "account"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["User-Agent"].startswith("codex_cli_rs/")


def test_missing_auth_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(OAuthResponsesError, match="Codex auth file not found"):
        oauth_headers()


def test_expired_token_refreshes_and_writes_back(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    payload = base64.urlsafe_b64encode(b'{"exp": 1}').decode().rstrip("=")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(json.dumps({"tokens": {"access_token": f"x.{payload}.x", "refresh_token": "old", "account_id": "account"}}))
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((json.loads(request.data), timeout))
        return BytesIO(json.dumps({"access_token": "new", "refresh_token": "rotated"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    headers = oauth_headers()
    assert headers["Authorization"] == "Bearer new"
    assert requests[0][0]["refresh_token"] == "old"
    assert json.loads(auth_file.read_text())["tokens"]["refresh_token"] == "rotated"
