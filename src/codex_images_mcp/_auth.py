"""Codex OAuth credentials: own store, device-code login and refresh.

Credential resolution, in order:

1. The codex-images store (``CODEX_IMAGES_AUTH_FILE`` or
   ``~/.config/codex-images/auth.json``), created by ``codex-images-mcp login``.
   It is its own OAuth session, so it is refreshed and rewritten here (mode 0600).
2. A borrowed Codex CLI login (``CODEX_HOME/auth.json``), used **read-only**
   while its access token is valid. It is never refreshed: refresh tokens rotate,
   and refreshing a borrowed copy would sign the owning Codex app out.

Token loading and refresh derived from codex-image-gen.
Copyright (c) 2026 Small Turtle 2. MIT License.
"""
from __future__ import annotations
import base64
import binascii
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
class OAuthResponsesError(Exception):
    """Codex OAuth credential or refresh failure."""

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body

_ISSUER = "https://auth.openai.com"
_DEFAULT_OAUTH_TOKEN_URL = f"{_ISSUER}/oauth/token"
_DEFAULT_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_DEVICE_TIMEOUT_SECONDS = 15 * 60
_LOGIN_HINT = "run `codex-images-mcp login`"

def own_auth_path() -> Path:
    configured = os.environ.get("CODEX_IMAGES_AUTH_FILE")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base).expanduser() / "codex-images" / "auth.json"


def oauth_headers(auth_file: str | os.PathLike[str] | None = None) -> dict[str, str]:
    """Return Codex request headers, refreshing only codex-images' own session."""
    own = Path(auth_file).expanduser() if auth_file is not None else own_auth_path()
    if own.exists():
        auth = _load_auth(own)
        tokens = _tokens(auth)
        if tokens.get("refresh_token") and _should_refresh(tokens.get("access_token"), _optional_str(auth.get("last_refresh"))):
            tokens = _merge_refresh(tokens, _refresh_tokens(tokens["refresh_token"]))
            auth["tokens"] = tokens
            auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            _write_private(own, auth)
        return _headers(tokens)
    borrowed = _borrowed_auth_path()
    if not borrowed.exists():
        raise OAuthResponsesError(f"No ChatGPT sign-in found; {_LOGIN_HINT}.")
    tokens = _tokens(_load_auth(borrowed))
    if _expired(tokens.get("access_token")):
        raise OAuthResponsesError(
            f"The borrowed Codex sign-in in {borrowed} has expired and is never refreshed here "
            f"(that would sign the Codex app out); {_LOGIN_HINT} to create a separate session.")
    return _headers(tokens)


def login_device(*, show: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep) -> Path:
    """Run OpenAI's device-code sign-in and save a private codex-images session."""
    started = _post_json(f"{_ISSUER}/api/accounts/deviceauth/usercode", {"client_id": _DEFAULT_OAUTH_CLIENT_ID},
                         not_found="Device-code sign-in is not enabled for this account. Enable device code "
                                   "authorization for Codex in ChatGPT settings (Security), then try again.")
    device_id, user_code = _optional_str(started.get("device_auth_id")), _optional_str(started.get("user_code") or started.get("usercode"))
    if not device_id or not user_code:
        raise OAuthResponsesError("Device-code response is missing the device id or user code.")
    interval = max(1, int(started.get("interval") or 5))
    show(f"Open {_ISSUER}/codex/device and enter the code {user_code} to sign in.")
    deadline = time.monotonic() + _DEVICE_TIMEOUT_SECONDS
    while True:
        if time.monotonic() > deadline:
            raise OAuthResponsesError("Device-code sign-in timed out after 15 minutes; start again.")
        try:
            granted = _post_json(f"{_ISSUER}/api/accounts/deviceauth/token", {"device_auth_id": device_id, "user_code": user_code})
            break
        except OAuthResponsesError as exc:
            if exc.status not in (403, 404):
                raise
            sleep(interval)
    code, verifier = _optional_str(granted.get("authorization_code")), _optional_str(granted.get("code_verifier"))
    if not code or not verifier:
        raise OAuthResponsesError("Device authorization response is missing the authorization code.")
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": code, "redirect_uri": f"{_ISSUER}/deviceauth/callback",
        "client_id": _DEFAULT_OAUTH_CLIENT_ID, "code_verifier": verifier,
    }).encode()
    issued = _request_json(_DEFAULT_OAUTH_TOKEN_URL, form, "application/x-www-form-urlencoded")
    if not isinstance(issued.get("access_token"), str):
        raise OAuthResponsesError("Token exchange did not return an access token.")
    tokens = _merge_refresh({}, issued)
    if not tokens.get("account_id"):
        raise OAuthResponsesError("Sign-in did not return a ChatGPT account id.")
    path = own_auth_path()
    _write_private(path, {"auth_mode": "chatgpt", "tokens": tokens,
                          "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())})
    return path


def logout() -> bool:
    """Delete codex-images' own session. Never touches a Codex CLI login."""
    path = own_auth_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _tokens(auth: Mapping[str, Any]) -> dict[str, Any]:
    tokens = auth.get("tokens")
    if not isinstance(tokens, Mapping):
        raise OAuthResponsesError("Auth file does not contain OAuth tokens.")
    return dict(tokens)


def _merge_refresh(tokens: Mapping[str, Any], issued: Mapping[str, Any]) -> dict[str, Any]:
    id_token = _optional_str(issued.get("id_token")) or _optional_str(tokens.get("id_token"))
    return {
        "id_token": id_token,
        "access_token": issued["access_token"],
        "refresh_token": _optional_str(issued.get("refresh_token")) or _optional_str(tokens.get("refresh_token")),
        "account_id": _account_id_from_id_token(id_token) or _optional_str(tokens.get("account_id")),
    }


def _headers(tokens: Mapping[str, Any]) -> dict[str, str]:
    access_token = _optional_str(tokens.get("access_token"))
    account_id = _optional_str(tokens.get("account_id")) or _account_id_from_id_token(_optional_str(tokens.get("id_token")))
    if not access_token:
        raise OAuthResponsesError(f"OAuth access token not found; {_LOGIN_HINT}.")
    if not account_id:
        raise OAuthResponsesError(f"ChatGPT account id not found; {_LOGIN_HINT}.")
    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.163.0 (Ubuntu 24.4.0; x86_64) xterm",
    }


def _expired(access_token: str | None) -> bool:
    if not access_token:
        return True
    exp = _jwt_claims(access_token).get("exp")
    return isinstance(exp, (int, float)) and exp <= time.time() + 300


def _post_json(url: str, payload: Mapping[str, Any], *, not_found: str | None = None) -> dict[str, Any]:
    try:
        return _request_json(url, json.dumps(payload).encode(), "application/json")
    except OAuthResponsesError as exc:
        if exc.status == 404 and not_found:
            raise OAuthResponsesError(not_found, status=404) from exc
        raise


def _request_json(url: str, body: bytes, content_type: str) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, method="POST",
                                     headers={"Content-Type": content_type, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise OAuthResponsesError(f"Sign-in request to {url} returned HTTP {exc.code}.", status=exc.code, body=text) from exc
    except urllib.error.URLError as exc:
        raise OAuthResponsesError(f"Sign-in request to {url} failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthResponsesError(f"Sign-in request to {url} returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise OAuthResponsesError(f"Sign-in request to {url} returned an unexpected payload.")
    return data


def _write_private(path: Path, auth: Mapping[str, Any]) -> None:
    """Atomically write a 0600 credential file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp already creates the file 0600 on POSIX; fchmod does not exist on Windows.
    fd, tmp = tempfile.mkstemp(prefix=".auth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), 0o600)
            json.dump(auth, handle, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load_auth(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise OAuthResponsesError(f"Auth file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthResponsesError(f"Codex auth file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise OAuthResponsesError(f"Codex auth file must contain a JSON object: {path}")
    return data


def _borrowed_auth_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _should_refresh(access_token: str | None, last_refresh: str | None) -> bool:
    if _expired(access_token):
        return True
    if last_refresh is None:
        return False
    value = last_refresh.replace("Z", "+00:00")
    try:
        refreshed_at = datetime.fromisoformat(value)
    except ValueError:
        return True
    if refreshed_at.tzinfo is None:
        refreshed_at = refreshed_at.replace(tzinfo=UTC)
    return refreshed_at.timestamp() <= time.time() - 55 * 60


def _refresh_tokens(refresh_token: str) -> dict[str, str]:
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _DEFAULT_OAUTH_CLIENT_ID,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        _DEFAULT_OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            refreshed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise OAuthResponsesError(
            f"Codex OAuth token refresh returned HTTP {exc.code}: {body_text}",
            status=exc.code,
            body=body_text,
        ) from exc
    except urllib.error.URLError as exc:
        raise OAuthResponsesError(
            f"Codex OAuth token refresh failed: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise OAuthResponsesError(
            "Codex OAuth token refresh returned invalid JSON"
        ) from exc

    if not isinstance(refreshed, dict) or not isinstance(
        refreshed.get("access_token"), str
    ):
        raise OAuthResponsesError(
            "Codex OAuth token refresh did not return access_token"
        )
    return refreshed


def _account_id_from_id_token(id_token: str | None) -> str | None:
    claims = _jwt_claims(id_token)
    auth_claim = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claim, Mapping):
        return _optional_str(auth_claim.get("chatgpt_account_id"))
    return None


def _jwt_claims(token: str | None) -> dict[str, Any]:
    if not token or "." not in token:
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        claims = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}



def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
