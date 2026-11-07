"""Tests for the stdio MCP adapter in :mod:`codex_images_mcp.mcp_server`."""
from __future__ import annotations

import base64
import hashlib
from io import BytesIO
from pathlib import Path

import pytest
from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent
from PIL import Image

from codex_images_mcp import mcp_server


def _engine(args):
    out = Path(args["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    path = out / "raw-0.png"
    Image.new("RGBA", (6, 4), (0, 128, 255, 255)).save(path)
    return {"ok": True, "images": [{"index": 0, "path": str(path)}], "warnings": [], "models": {}}


def _structured(result: CallToolResult) -> dict:
    assert result.structuredContent is not None
    return result.structuredContent


@pytest.fixture(autouse=True)
def _output_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path))
    monkeypatch.delenv("CODEX_IMAGES_PUBLIC_BASE_URL", raising=False)


def test_server_name_is_codex_images():
    assert mcp_server.app.name == "codex-images"


def test_lists_generation_and_artifact_tools():
    tools = mcp_server.tools()
    assert [tool.name for tool in tools] == ["codex_images", "codex_images_artifact"]
    assert "codex-images.result/v2" in (tools[0].description or "")
    assert tools[0].inputSchema["required"] == ["prompt"]
    assert tools[1].inputSchema["required"] == ["uri"]


def test_generation_returns_resource_link_and_local_path(tmp_path):
    result = mcp_server.call_tool("codex_images", {"prompt": "blue"}, engine=_engine)
    assert result.isError is False
    assert isinstance(result.content[0], TextContent)
    link = result.content[1]
    assert isinstance(link, ResourceLink)
    artifact = _structured(result)["artifacts"][0]
    assert str(link.uri) == artifact["uri"]
    assert artifact["uri"].startswith("mcp://codex-images/jobs/")
    path = Path(artifact["path"])
    assert path.is_relative_to(tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact["sha256"]


def test_artifact_tool_and_resource_return_the_same_bytes():
    generated = mcp_server.call_tool("codex_images", {"prompt": "blue"}, engine=_engine)
    uri = _structured(generated)["artifacts"][0]["uri"]
    result = mcp_server.call_tool("codex_images_artifact", {"uri": uri})
    assert result.isError is False
    assert _structured(result)["schema_version"] == "codex-images.artifact/v1"
    image = result.content[1]
    assert isinstance(image, ImageContent) and image.mimeType == "image/png"
    data = base64.b64decode(image.data)
    assert mcp_server.read_resource(uri)[0].content == data
    with Image.open(BytesIO(data)) as decoded:
        assert decoded.size == (6, 4)


def test_failed_input_is_an_error_result_not_an_exception():
    result = mcp_server.call_tool("codex_images", {"prompt": "x", "references": ["/etc/hosts"]}, engine=_engine)
    assert result.isError is True
    assert _structured(result)["errors"][0]["code"] == "LOCAL_PATH_FORBIDDEN"


def test_unknown_tool_is_an_error_result():
    result = mcp_server.call_tool("imagen", {"prompt": "x"}, engine=_engine)
    assert result.isError is True
    assert _structured(result)["errors"][0]["code"] == "UNKNOWN_TOOL"


def test_unknown_resource_raises_a_typed_error():
    with pytest.raises(ValueError, match="ARTIFACT_READ_FAILED"):
        mcp_server.read_resource("mcp://codex-images/jobs/nope/artifacts/0")
