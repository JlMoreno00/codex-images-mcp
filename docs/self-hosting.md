# Self-hosting

Run one codex-images server, for example on a VPS, and use it from every machine and agent you work with. Images are generated with the ChatGPT session you sign in with on the server.

## Docker Compose

```bash
git clone https://github.com/JlMoreno00/codex-images-mcp && cd codex-images-mcp
export CODEX_IMAGES_HTTP_TOKEN=$(openssl rand -hex 32)   # keep it, clients need it
docker compose up -d
docker compose run --rm codex-images codex-images-mcp login
```

The example compose file publishes the server only on `127.0.0.1:8324` and keeps the session and the artifacts in `./data`. To build from source instead of pulling the image, use `docker compose up -d --build`.

## Reverse proxy

Expose the server through HTTPS. With Caddy:

```text
images.example.com {
    reverse_proxy 127.0.0.1:8324
}
```

Clients connect to `https://images.example.com/mcp/` with the header `Authorization: Bearer <token>`. Generation can take minutes, so allow long response times on the proxy.

For large artifacts, set `CODEX_IMAGES_PUBLIC_BASE_URL=https://images.example.com`. `codex_images_artifact` can then hand out signed download links on `/download/<token>`. Those links take no bearer: the token in the URL is the credential, single-use and valid for at most 120 seconds.

## Without Docker

```bash
uv tool install 'codex-images-mcp[http]'
codex-images-mcp login
CODEX_IMAGES_HTTP_TOKEN=... codex-images-mcp-http
```

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `CODEX_IMAGES_HTTP_TOKEN` | unset | Bearer token required on `/mcp`. Required to listen on a non-loopback address. |
| `CODEX_IMAGES_HTTP_HOST` | `127.0.0.1` (`0.0.0.0` in Docker) | Listen address. |
| `CODEX_IMAGES_HTTP_PORT` | `8324` | Listen port. |
| `CODEX_IMAGES_PUBLIC_BASE_URL` | unset | Public HTTPS origin used for signed downloads. Downloads are disabled when it is unset. |
| `CODEX_IMAGES_DOWNLOAD_TTL_SECONDS` | `120` | Download link lifetime, capped at 300. |
| `CODEX_IMAGES_OUTPUT_DIR` | user data dir (`/data/out` in Docker) | Artifact store. |
| `CODEX_IMAGES_AUTH_FILE` | `~/.config/codex-images/auth.json` (`/data/auth.json` in Docker) | codex-images ChatGPT session. |
| `CODEX_HOME` | `~/.codex` | Read-only fallback to a Codex CLI login. |
| `CODEX_IMAGES_MAX_INLINE_INPUT_BYTES` | 8 MiB | Per-file input limit. |
| `CODEX_IMAGES_MAX_REQUEST_INPUT_BYTES` | 24 MiB | Total inline input per request. |

## Security model

- `/mcp` requires the bearer token when one is set. The server refuses to start on a non-loopback address without a token.
- The server never reads client paths. Inputs arrive as inline bytes, public HTTPS URLs or earlier artifacts.
- URL references must be HTTPS and resolve only to public addresses. The server connects to the address it checked, so a DNS change after the check cannot redirect the request. It does not follow redirects and ignores proxy environment variables.
- Artifact URIs are random, are not listed by `resources/list`, are confined to their job directory, and are verified (SHA-256, MIME, dimensions) every time they are read.
- Results never include server-side paths.
- Anyone with the bearer token can generate images on your ChatGPT plan, so treat the token like your password.

## Clients

- **Pi / Gentle Shell**: install [`pi-codex-images`](../packages/pi-codex-images) and point its `endpoint` at your server.
- **Other MCP clients** that support Streamable HTTP with custom headers: add the URL and the `Authorization` header in their MCP configuration.
