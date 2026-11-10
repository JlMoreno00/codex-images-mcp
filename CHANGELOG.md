# Changelog

## 0.1.1 — 2026-11-10

### Added

- README figures of real gpt-image-2 output from this server: a transparent generate, the masked edit (a stamped coffee bean), and a checkerboard view of the alpha channel.
- Official MCP registry metadata for `io.github.JlMoreno00/codex-images-mcp`: the `mcp-name` marker in the README, `server.json` (PyPI over stdio, GHCR over self-hosted Streamable HTTP), and the OCI image label the registry uses to verify the image.
- `pi.image` on `pi-codex-images`, pointing at the generate/edit figure, so the pi.dev gallery has a preview.

### Changed

- Root LICENSE is the standard MIT text, with copyrights for wauwa and JlMoreno00, so license detection can see MIT. The codex-image-gen attribution moved to NOTICE, which still credits the VelmoAI/codex-imagen fork (wauwa, MIT) and the OAuth handling derived from smturtle2/codex-image-gen.
- `packages/pi-codex-images/LICENSE` adds the JlMoreno00 copyright beside wauwa.

## 0.1.0 — 2026-11-09

First release of codex-images-mcp, a fork of [VelmoAI/codex-imagen](https://github.com/VelmoAI/codex-imagen).

### Added

- Native Codex Images engine (`/backend-api/codex/images/{generations,edits}`, gpt-image-2): prompts sent verbatim, native transparency, references and masks.
- `codex_images` and `codex_images_artifact` MCP tools returning `codex-images.result/v2` with verified `resource_link` artifacts (SHA-256, MIME, dimensions).
- Output size policies `native`, `exact`, `fit` and `cover`.
- Streamable HTTP server `codex-images-mcp-http`: built-in bearer auth, refuses a public bind without a token, signed single-use downloads.
- Docker image and example compose file.
- `codex-images-mcp login` / `logout`: a device-code ChatGPT session of its own, stored with mode 0600.
- SSRF-hardened remote references: HTTPS, public addresses only, pinned IP, no redirects.
- Pi / Gentle Shell extension `pi-codex-images`.
- Optional OpenAI API engine (`CODEX_IMAGES_ENGINE=api` + `OPENAI_API_KEY`) that honors `model` (default `gpt-image-2.6-flare`), `quality` and `complexity`; the plan route ignores them with a warning. Results report the engine in `models.engine`.
- CI on Linux, macOS and Windows; releases to PyPI, GHCR and npm.

### Changed

- Package, CLI and MCP server renamed to `codex-images-mcp` / `codex-images`. `setup` keeps existing `codex-imagen` entries and explains how to remove them.
- A borrowed Codex CLI login (`CODEX_HOME/auth.json`) is used read-only and never refreshed.

### Removed

- The prompt-rewriting model, reasoning modes, skill-file injection and chroma-key pipeline. Their input fields are accepted and ignored with a warning.
- The Python SDK (`imagen`, `ImagenOptions`, `ImagenResult`) and the `imagen` console script.
- The `codex-image-gen` dependency.
