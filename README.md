# codex-images-mcp

Generate and edit images from any MCP client with **gpt-image-2 on the ChatGPT plan you already pay for**. No OpenAI API key, no per-image bill.

codex-images-mcp is an MCP server for the Codex Images endpoint. It runs locally over stdio, or as a **self-hosted HTTP server** that your laptop, your VPS agents and your Pi / Gentle Shell sessions can share. Every image comes back as a verifiable artifact: SHA-256, byte count, MIME type and real dimensions.

> **Unofficial.** Not affiliated with or endorsed by OpenAI. It signs in with your own ChatGPT account and uses the same endpoint as the Codex CLI. You are bound by OpenAI's terms for your account, and image generation counts against your plan's limits.

## What you get

- **Two tools**: `codex_images` (generate/edit) and `codex_images_artifact` (fetch an image by URI, inline or through a short-lived signed download).
- **Batches**: `parallel`, `variants`, `chain` and `branded-parallel` (every image references a shared anchor for brand-consistent sets).
- **Edits with references and masks**: up to five references per request, HTTPS URLs or inline images.
- **Native transparency**: real alpha PNGs from the model, with a warning when a result comes back opaque.
- **Exact output sizes**: `native`, `exact`, `fit` or `cover` post-processing, so a 1200×630 card is 1200×630.
- **Prompts sent verbatim**: no hidden rewriting model in between.
- **Self-hosting that is safe by default**: built-in bearer auth, refuses to listen publicly without a token, SSRF-hardened URL fetching (HTTPS, public addresses only, pinned IP, no redirects), no server paths in results.
- **Its own ChatGPT session**: `codex-images-mcp login` uses a device code and keeps a separate session, so it never rotates (and breaks) your Codex app's sign-in.

## Quick start (local, stdio)

```bash
uvx --from codex-images-mcp codex-images-mcp login     # sign in once (device code)
uvx --from codex-images-mcp codex-images-mcp setup     # register in Claude Code, Claude Desktop, Codex, Cursor, OpenCode
```

Or try it from the terminal:

```bash
uvx --from codex-images-mcp codex-images-mcp generate "a ceramic mug, studio light" --transparent --pretty
```

Already signed in to the Codex CLI? codex-images-mcp can use that login **read-only** until its token expires. It never refreshes it; run `login` for a session of its own.

## Self-hosted (HTTP)

```bash
export CODEX_IMAGES_HTTP_TOKEN=$(openssl rand -hex 32)
docker compose up -d
docker compose run --rm codex-images codex-images-mcp login
```

Then point your clients at `https://your-host/mcp/` with `Authorization: Bearer $CODEX_IMAGES_HTTP_TOKEN`. See [docs/self-hosting.md](docs/self-hosting.md) for the reverse proxy, signed downloads and every setting.

## Pi and Gentle Shell

[`pi-codex-images`](packages/pi-codex-images) is a Pi extension for a self-hosted server. It reads the local files you authorize, sends them inline, and writes verified results atomically into your project:

```bash
pi install npm:pi-codex-images
```

## Documentation

- [MCP tools reference](docs/mcp.md): inputs, results, error codes.
- [CLI](docs/cli.md): `generate`, `login`, `logout`, `setup`, `status`, `uninstall`.
- [Self-hosting](docs/self-hosting.md): Docker, reverse proxy, configuration, security model.
- [Examples](docs/examples.md): batches, edits, masks, transparency, exact sizes.
- [Comparison](docs/comparison.md): how this differs from similar projects.

## Credits

codex-images-mcp started as a fork of [VelmoAI/codex-imagen](https://github.com/VelmoAI/codex-imagen) by wauwa. The installer and much of the original structure come from there. The OAuth token handling derives from [codex-image-gen](https://github.com/smturtle2/codex-image-gen) by Small Turtle 2. The generation engine was replaced with the native Codex Images endpoint, and the prompt-rewriting and chroma-key layers were removed.

## License

MIT. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
