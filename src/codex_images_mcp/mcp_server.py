"""stdio MCP server exposing ``codex_images`` and ``codex_images_artifact``.

Both tools delegate to the transport-neutral :mod:`codex_images_mcp._service`.
Over stdio the client shares the machine, so results carry the local file path
of every artifact besides the ``mcp://codex-images/...`` resource links.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, Resource, ResourceLink, TextContent, Tool

from codex_images_mcp import __version__, _native, _service
from codex_images_mcp._schema import TOOL_DESCRIPTION

SERVER_NAME = "codex-images"
TOOL_NAME = "codex_images"
ARTIFACT_TOOL_NAME = "codex_images_artifact"
ARTIFACT_DESCRIPTION = (
    "Retrieve a codex_images artifact by its mcp://codex-images/... URI. Returns verified metadata "
    "(SHA-256, bytes, MIME, dimensions) and the image inline, or a signed download URL when the "
    "server has a public HTTPS base URL and delivery=download is requested."
)

app: Server = Server(SERVER_NAME, version=__version__)


def generation_result(result: dict[str, Any], *, include_paths: bool) -> CallToolResult:
    """Convert a service generation result into an MCP tool result."""
    structured = dict(result["structured"])
    if include_paths:
        paths = {item["index"]: item["path"] for item in result["artifacts"]}
        structured["artifacts"] = [
            {**item, "path": paths[item["index"]]} if item["index"] in paths else item
            for item in structured["artifacts"]
        ]
    content: list[Any] = [TextContent(type="text", text=result["summary"])]
    for item in result["artifacts"]:
        content.append(ResourceLink(type="resource_link", uri=item["uri"], name=item["name"],
                                    mimeType=item["mime_type"], size=item["bytes"]))
    return CallToolResult(isError=result["is_error"], content=content, structuredContent=structured)


def artifact_result(result: dict[str, Any]) -> CallToolResult:
    """Convert a service artifact result into an MCP tool result."""
    if result["is_error"]:
        return CallToolResult(isError=True, content=[TextContent(type="text", text=result["summary"])],
                              structuredContent=result["structured"])
    metadata = result["metadata"]
    content: list[Any] = [TextContent(type="text", text=json.dumps(metadata))]
    if result["data"] is not None:
        content.append(ImageContent(type="image", data=base64.b64encode(result["data"]).decode("ascii"),
                                    mimeType=metadata["mime_type"]))
    return CallToolResult(isError=False, content=content, structuredContent=metadata)


def tools() -> list[Tool]:
    return [
        Tool(name=TOOL_NAME, description=TOOL_DESCRIPTION, inputSchema=_service.V2_INPUT_SCHEMA),
        Tool(name=ARTIFACT_TOOL_NAME, description=ARTIFACT_DESCRIPTION, inputSchema=_service.ARTIFACT_INPUT_SCHEMA),
    ]


def call_tool(name: str, arguments: dict[str, Any], *, engine=None, include_paths: bool = True) -> CallToolResult:
    if name == TOOL_NAME:
        return generation_result(_service.generate(arguments or {}, engine or _native.generate), include_paths=include_paths)
    if name == ARTIFACT_TOOL_NAME:
        return artifact_result(_service.get_artifact(arguments or {}))
    error = _service.typed_error("UNKNOWN_TOOL", f"Unknown tool {name!r}.")
    return CallToolResult(isError=True, content=[TextContent(type="text", text=error["message"])],
                          structuredContent=_service.failure(error)["structured"])


def read_resource(uri: str) -> list[ReadResourceContents]:
    try:
        data, metadata, _remaining = _service.read_artifact(_service.output_root(), uri)
    except _service.ServiceError as exc:
        raise ValueError(f"{exc.code}: {exc}") from exc
    return [ReadResourceContents(content=data, mime_type=metadata["mime_type"])]


@app.list_tools()
async def _list_tools() -> list[Tool]:
    return tools()


@app.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
    # The engine blocks on HTTP; keep the event loop responsive.
    return await asyncio.to_thread(call_tool, name, arguments)


@app.list_resources()
async def _list_resources() -> list[Resource]:
    return []


@app.read_resource()
async def _read_resource(uri: Any) -> list[ReadResourceContents]:
    return read_resource(str(uri))


async def _serve() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> int:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
