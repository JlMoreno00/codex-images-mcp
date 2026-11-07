"""Codex OAuth credentials and refresh.

Derived from codex-image-gen. Copyright (c) 2026 Small Turtle 2. MIT License.

TODO(T4): Writing back borrowed auth.json is a release blocker: refresh-token
rotation can sign out the owning Codex app. Use a dedicated credential store.
"""
from __future__ import annotations
import base64
import binascii
import json
import os
import time
import urllib.error
import urllib.request
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

_DEFAULT_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
_DEFAULT_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

def oauth_headers(auth_file: str | os.PathLike[str] | None = None) -> dict[str, str]:
    auth = _load_auth(auth_file)
    tokens = auth.get("tokens")
    if not isinstance(tokens, Mapping):
        raise OAuthResponsesError("Codex auth file does not contain OAuth tokens")

    access_token = _optional_str(tokens.get("access_token"))
    refresh_token = _optional_str(tokens.get("refresh_token"))
    id_token = _optional_str(tokens.get("id_token"))
    account_id = _optional_str(tokens.get("account_id")) or _account_id_from_id_token(
        id_token
    )

    last_refresh = _optional_str(auth.get("last_refresh"))
    if refresh_token and _should_refresh(access_token, last_refresh):
        refreshed = _refresh_tokens(refresh_token)
        tokens = {
            "id_token": refreshed.get("id_token") or id_token,
            "access_token": refreshed["access_token"],
            "refresh_token": refreshed.get("refresh_token") or refresh_token,
            "account_id": _account_id_from_id_token(refreshed.get("id_token"))
            or account_id,
        }
        auth["tokens"] = tokens
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        _write_auth(auth_file, auth)
        access_token = _optional_str(tokens.get("access_token"))
        account_id = _optional_str(tokens.get("account_id"))

    if not access_token:
        raise OAuthResponsesError(
            "Codex OAuth access token not found; run `codex login`"
        )
    if not account_id:
        raise OAuthResponsesError("Codex OAuth account id not found; run `codex login`")

    return {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "User-Agent": "codex_cli_rs/0.163.0 (Ubuntu 24.4.0; x86_64) xterm",
    }


def _load_auth(auth_file: str | os.PathLike[str] | None) -> dict[str, Any]:
    path = _auth_file_path(auth_file)
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise OAuthResponsesError(f"Codex auth file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise OAuthResponsesError(f"Codex auth file is not valid JSON: {path}") from exc
    if not isinstance(data, dict):
        raise OAuthResponsesError(f"Codex auth file must contain a JSON object: {path}")
    return data


def _write_auth(
    auth_file: str | os.PathLike[str] | None,
    auth: Mapping[str, Any],
) -> None:
    path = _auth_file_path(auth_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    path.chmod(0o600)


def _auth_file_path(auth_file: str | os.PathLike[str] | None) -> Path:
    if auth_file is not None:
        return Path(auth_file).expanduser()
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _should_refresh(access_token: str | None, last_refresh: str | None) -> bool:
    if not access_token:
        return True
    claims = _jwt_claims(access_token)
    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and exp <= time.time() + 300:
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
            "scope": "openid profile email offline_access",
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
