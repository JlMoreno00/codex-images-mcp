"""Tests for the Streamable HTTP server in :mod:`codex_images_mcp.http_server`."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from starlette.testclient import TestClient

from codex_images_mcp import _service, http_server

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _engine(args):
    out = Path(args["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    path = out / "raw-0.png"
    Image.new("RGBA", (5, 5), (9, 9, 9, 255)).save(path)
    return {"ok": True, "images": [{"index": 0, "path": str(path)}], "warnings": [], "models": {}}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("CODEX_IMAGES_PUBLIC_BASE_URL", "https://images.example.com")


def _sse_json(text: str) -> dict:
    data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1] if data else text)


def test_mcp_requires_bearer_token():
    with TestClient(http_server.create_app(token="s3cret", engine=_engine)) as client:
        assert client.post("/mcp/", json=INIT, headers=HEADERS).status_code == 401
        wrong = client.post("/mcp/", json=INIT, headers={**HEADERS, "Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = client.post("/mcp/", json=INIT, headers={**HEADERS, "Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert _sse_json(ok.text)["result"]["serverInfo"]["name"] == "codex-images"


def test_mcp_without_token_is_open_for_loopback_use():
    with TestClient(http_server.create_app(token="", engine=_engine)) as client:
        assert client.post("/mcp/", json=INIT, headers=HEADERS).status_code == 200


def test_signed_download_serves_artifact_once(tmp_path):
    generated = _service.generate({"prompt": "x"}, _engine, output_dir=tmp_path)
    uri = generated["structured"]["artifacts"][0]["uri"]
    url = _service.get_artifact({"uri": uri, "delivery": "download"})["metadata"]["download"]["url"]
    path = url.removeprefix("https://images.example.com")
    with TestClient(http_server.create_app(token="s3cret", engine=_engine)) as client:
        first = client.get(path)
        assert first.status_code == 200
        assert first.headers["content-type"] == "image/png"
        assert first.headers["cache-control"] == "private, no-store"
        assert first.content == Path(generated["artifacts"][0]["path"]).read_bytes()
        assert client.get(path).status_code == 404


@pytest.mark.parametrize("token", ["nope", "a" * 43 + "." + "0" * 64])
def test_bad_download_tokens_are_404(token):
    with TestClient(http_server.create_app(token="s3cret", engine=_engine)) as client:
        assert client.get(f"/download/{token}").status_code == 404


def test_no_static_file_mount(tmp_path):
    _service.generate({"prompt": "x"}, _engine, output_dir=tmp_path)
    with TestClient(http_server.create_app(token="s3cret", engine=_engine)) as client:
        assert client.get("/files/").status_code == 404


def test_http_results_do_not_leak_server_paths(tmp_path):
    result = http_server.mcp_server.call_tool("codex_images", {"prompt": "x"}, engine=_engine, include_paths=False)
    assert result.structuredContent is not None
    assert str(tmp_path) not in json.dumps(result.structuredContent)


def test_refuses_public_bind_without_token(monkeypatch, capsys):
    monkeypatch.delenv("CODEX_IMAGES_HTTP_TOKEN", raising=False)
    monkeypatch.setenv("CODEX_IMAGES_HTTP_HOST", "0.0.0.0")
    assert http_server.main() == 2
    assert "Refusing to listen on 0.0.0.0 without CODEX_IMAGES_HTTP_TOKEN" in capsys.readouterr().err
