"""Streamable HTTP MCP server for self-hosting codex-images.

Serves the same ``codex_images`` and ``codex_images_artifact`` tools as the
stdio server, on top of :mod:`codex_images_mcp._service`.

Routes:

* ``/mcp`` — MCP Streamable HTTP endpoint. Requires ``Authorization: Bearer``
  with ``CODEX_IMAGES_HTTP_TOKEN`` when that variable is set.
* ``/download/<token>`` — signed, single-use, short-lived artifact downloads
  issued by ``codex_images_artifact`` when ``CODEX_IMAGES_PUBLIC_BASE_URL`` is
  set. The token itself is the capability, so this route takes no bearer.

The server refuses to listen on a non-loopback address without a token.
Results never include server-side file paths.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import os
import re
import sys
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, Resource, Tool
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from codex_images_mcp import __version__, _native, _service, mcp_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8324
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}\.[0-9a-f]{64}")


def build_server(engine=None) -> Server:
    server: Server = Server(mcp_server.SERVER_NAME, version=__version__)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return mcp_server.tools()

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult:
        return await asyncio.to_thread(
            mcp_server.call_tool, name, arguments, engine=engine or _native.generate, include_paths=False)

    @server.list_resources()
    async def _list_resources() -> list[Resource]:
        # Job resources are capability URIs and intentionally non-enumerable.
        return []

    @server.read_resource()
    async def _read_resource(uri: Any) -> list[ReadResourceContents]:
        return mcp_server.read_resource(str(uri))

    return server


class BearerAuth:
    """Reject requests without the configured bearer token."""

    def __init__(self, app: ASGIApp, token: str | None):
        self.app = app
        self.expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.expected is not None and scope["type"] == "http":
            supplied = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                response = Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


async def _download(request) -> Response:
    token = request.path_params["token"]
    try:
        if not _TOKEN_RE.fullmatch(token):
            raise _service.ServiceError("DOWNLOAD_UNAVAILABLE", "Malformed token.")
        uri = _service.redeem_download(token)
        data, metadata, _remaining = _service.read_artifact(_service.output_root(), uri)
    except _service.ServiceError:
        return Response(status_code=404)
    return Response(data, media_type=metadata["mime_type"], headers={
        "Content-Disposition": f'attachment; filename="{metadata["filename"]}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


def create_app(*, token: str | None = None, engine=None) -> Starlette:
    """Build the ASGI app. ``token`` defaults to ``CODEX_IMAGES_HTTP_TOKEN``."""
    token = token if token is not None else (os.environ.get("CODEX_IMAGES_HTTP_TOKEN") or None)
    manager = StreamableHTTPSessionManager(app=build_server(engine), json_response=False, stateless=False)

    async def mcp_endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with manager.run():
            yield

    return Starlette(
        routes=[
            Route("/download/{token}", endpoint=_download, methods=["GET"]),
            Mount("/mcp", app=BearerAuth(mcp_endpoint, token)),
        ],
        lifespan=lifespan,
    )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main() -> int:
    import uvicorn

    host = os.environ.get("CODEX_IMAGES_HTTP_HOST", DEFAULT_HOST)
    port = int(os.environ.get("CODEX_IMAGES_HTTP_PORT", str(DEFAULT_PORT)))
    if not os.environ.get("CODEX_IMAGES_HTTP_TOKEN") and not _is_loopback(host):
        print(f"Refusing to listen on {host} without CODEX_IMAGES_HTTP_TOKEN. "
              "Set a token or bind to 127.0.0.1.", file=sys.stderr)
        return 2
    uvicorn.run(create_app(), host=host, port=port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
