"""Credential resolution, refresh and device-code login (no network)."""
import base64
import json
import stat
import sys
from email.message import Message
from io import BytesIO

import pytest

from codex_images_mcp import _auth
from codex_images_mcp._auth import OAuthResponsesError, login_device, logout, oauth_headers


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"h.{payload}.s"


FRESH = _jwt({"exp": 4102444800})  # year 2100
EXPIRED = _jwt({"exp": 1})
ID_TOKEN = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}})


class FakeHTTP:
    """Replace urllib.request.urlopen with scripted responses keyed by URL suffix."""

    def __init__(self, monkeypatch, script):
        self.script = {key: list(value) for key, value in script.items()}
        self.requests = []
        monkeypatch.setattr("urllib.request.urlopen", self)

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append((url, request.data))
        for suffix, responses in self.script.items():
            if url.endswith(suffix):
                status, body = responses.pop(0)
                if status >= 400:
                    raise _auth.urllib.error.HTTPError(url, status, "x", Message(), BytesIO(json.dumps(body).encode()))
                return BytesIO(json.dumps(body).encode())
        raise AssertionError(f"unexpected request {url}")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    own = tmp_path / "own" / "auth.json"
    codex = tmp_path / "codex"
    codex.mkdir()
    monkeypatch.setenv("CODEX_IMAGES_AUTH_FILE", str(own))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return own, codex / "auth.json"


def _write(path, tokens, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"tokens": tokens, **extra}))


def test_own_store_wins_and_returns_codex_headers(homes):
    own, borrowed = homes
    _write(own, {"access_token": FRESH, "refresh_token": "r", "account_id": "own-acct"})
    _write(borrowed, {"access_token": "borrowed", "account_id": "borrowed-acct"})
    headers = oauth_headers()
    assert headers["Authorization"] == f"Bearer {FRESH}"
    assert headers["chatgpt-account-id"] == "own-acct"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["User-Agent"].startswith("codex_cli_rs/")


def test_own_store_refreshes_and_rewrites_only_itself(homes, monkeypatch):
    own, borrowed = homes
    _write(own, {"access_token": EXPIRED, "refresh_token": "old", "account_id": "acct"})
    _write(borrowed, {"access_token": EXPIRED, "refresh_token": "codex-app", "account_id": "acct"})
    before = borrowed.read_bytes()
    http = FakeHTTP(monkeypatch, {"/oauth/token": [(200, {"access_token": FRESH, "refresh_token": "rotated"})]})
    headers = oauth_headers()
    assert headers["Authorization"] == f"Bearer {FRESH}"
    assert json.loads(http.requests[0][1])["refresh_token"] == "old"
    assert json.loads(own.read_text())["tokens"]["refresh_token"] == "rotated"
    if sys.platform != "win32":  # POSIX permission bits only
        assert stat.S_IMODE(own.stat().st_mode) == 0o600
    assert borrowed.read_bytes() == before


def test_borrowed_codex_login_is_read_only_while_valid(homes, monkeypatch):
    _own, borrowed = homes
    _write(borrowed, {"access_token": FRESH, "refresh_token": "codex-app", "account_id": "acct"})
    before = borrowed.read_bytes()
    http = FakeHTTP(monkeypatch, {})
    assert oauth_headers()["Authorization"] == f"Bearer {FRESH}"
    assert http.requests == []
    assert borrowed.read_bytes() == before


def test_expired_borrowed_login_is_never_refreshed(homes, monkeypatch):
    _own, borrowed = homes
    _write(borrowed, {"access_token": EXPIRED, "refresh_token": "codex-app", "account_id": "acct"})
    before = borrowed.read_bytes()
    http = FakeHTTP(monkeypatch, {})
    with pytest.raises(OAuthResponsesError, match="run `codex-images-mcp login`"):
        oauth_headers()
    assert http.requests == []
    assert borrowed.read_bytes() == before


def test_no_credentials_points_to_login(homes):
    with pytest.raises(OAuthResponsesError, match="run `codex-images-mcp login`"):
        oauth_headers()


def test_device_login_saves_private_store(homes, monkeypatch):
    own, borrowed = homes
    http = FakeHTTP(monkeypatch, {
        "/api/accounts/deviceauth/usercode": [(200, {"device_auth_id": "dev-1", "user_code": "ABCD-1234", "interval": 1})],
        "/api/accounts/deviceauth/token": [(403, {"error": "pending"}), (200, {"authorization_code": "code-1", "code_verifier": "ver-1"})],
        "/oauth/token": [(200, {"id_token": ID_TOKEN, "access_token": FRESH, "refresh_token": "mine"})],
    })
    shown, sleeps = [], []
    path = login_device(show=shown.append, sleep=sleeps.append)
    assert path == own
    assert "https://auth.openai.com/codex/device" in shown[0] and "ABCD-1234" in shown[0]
    assert sleeps == [1]
    exchange = http.requests[-1][1].decode()
    assert "grant_type=authorization_code" in exchange and "code_verifier=ver-1" in exchange
    assert "redirect_uri=https%3A%2F%2Fauth.openai.com%2Fdeviceauth%2Fcallback" in exchange
    saved = json.loads(own.read_text())["tokens"]
    assert saved == {"id_token": ID_TOKEN, "access_token": FRESH, "refresh_token": "mine", "account_id": "acct-1"}
    if sys.platform != "win32":  # POSIX permission bits only
        assert stat.S_IMODE(own.stat().st_mode) == 0o600
    assert not borrowed.exists()


def test_device_login_not_enabled_explains_setting(homes, monkeypatch):
    FakeHTTP(monkeypatch, {"/api/accounts/deviceauth/usercode": [(404, {})]})
    with pytest.raises(OAuthResponsesError, match="device code authorization"):
        login_device(show=lambda _m: None, sleep=lambda _s: None)


def test_logout_removes_only_own_store(homes):
    own, borrowed = homes
    _write(own, {"access_token": FRESH, "account_id": "acct"})
    _write(borrowed, {"access_token": FRESH, "account_id": "acct"})
    assert logout() is True
    assert not own.exists() and borrowed.exists()
    assert logout() is False


def test_private_write_works_without_fchmod(homes, monkeypatch):
    """Windows has no os.fchmod; the store must still be written and no temp file left behind."""
    own, _borrowed = homes
    monkeypatch.delattr(_auth.os, "fchmod", raising=False)
    _auth._write_private(own, {"tokens": {"access_token": FRESH, "account_id": "acct"}})
    assert json.loads(own.read_text())["tokens"]["account_id"] == "acct"
    assert [p.name for p in own.parent.iterdir()] == ["auth.json"]
