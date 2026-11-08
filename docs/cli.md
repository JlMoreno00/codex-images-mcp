# CLI reference

```text
codex-images-mcp generate   Generate or edit images
codex-images-mcp login      Sign in to ChatGPT with a device code
codex-images-mcp logout     Delete the codex-images session
codex-images-mcp setup      Register the MCP server in your AI clients
codex-images-mcp status     Show detected clients and install state
codex-images-mcp uninstall  Remove the registration
```

The servers have their own commands: `codex-images-mcp-server` (stdio) and `codex-images-mcp-http` (HTTP, see [self-hosting](self-hosting.md)).

## `login` / `logout`

```bash
codex-images-mcp login
# Open https://auth.openai.com/codex/device and enter the code ABCD-1234 to sign in.
# Signed in. Session saved to ~/.config/codex-images/auth.json
```

`login` uses OpenAI's device-code sign-in, so it also works over SSH or inside a container. If it reports that device-code sign-in is not enabled, turn on device code authorization for Codex in ChatGPT's security settings.

The session is stored with mode `0600` in `~/.config/codex-images/auth.json` (or `$XDG_CONFIG_HOME/codex-images/auth.json`, or `CODEX_IMAGES_AUTH_FILE`). It is separate from the Codex CLI's session. Without it, codex-images-mcp falls back to `CODEX_HOME/auth.json` (default `~/.codex/auth.json`), but only **read-only** and only while that token is valid. It never refreshes a borrowed login, because refresh tokens rotate and doing so would sign the Codex app out.

`logout` deletes only the codex-images session.

## `generate`

```bash
codex-images-mcp generate "a ceramic mug, studio light" --transparent
codex-images-mcp generate --prompt-json spec.json -n 4
codex-images-mcp generate "make it night" -r photo.jpg --mask sky.png
codex-images-mcp generate "og card" --width 1200 --height 630 --size-policy cover --format jpeg
CODEX_IMAGES_ENGINE=api OPENAI_API_KEY=sk-... codex-images-mcp generate "poster" --model gpt-image-2.6-sunburst --quality max
```

| Option | Meaning |
|---|---|
| `PROMPT` | Prompt text. |
| `--prompt-json FILE` | JSON file with a prompt string, labeled-spec object or list of prompts. |
| `-r, --reference FILE` | Reference image (repeatable, up to five). |
| `--mask FILE` | Edit mask; requires `--reference`. |
| `-n, --count N` | N variants of one prompt. |
| `--transparent` | Request a native transparent background. |
| `--format png\|jpeg\|webp` | Output format (default `png`). |
| `--width`, `--height` | Final dimensions. |
| `--size-policy native\|exact\|fit\|cover` | How to reach those dimensions (default `native`). |
| `--model ID` | GPT Image model, for example `gpt-image-2.6-sunburst` (API engine only). |
| `--quality LEVEL` | `auto`, `low`, `medium`, `high`, `xhigh` or `max` (API engine only). |
| `--complexity LEVEL` | `low`, `medium` or `high` (API engine only). |
| `-o, --output-dir DIR` | Artifact store (default `CODEX_IMAGES_OUTPUT_DIR` or your user data directory). |
| `--pretty` | Human summary instead of JSON. |

Stdout gets the `codex-images.result/v2` JSON, with a local `path` for each image, or a short summary with `--pretty`. Warnings and errors go to stderr in `--pretty` mode.

Exit codes: `0` when every image completed, `1` for sign-in or other command failures, `2` when generation failed or was partial, `3` for invalid arguments.

## `setup` / `status` / `uninstall`

```bash
codex-images-mcp setup                     # interactive
codex-images-mcp setup --all               # every detected client
codex-images-mcp setup --client cursor     # one client
codex-images-mcp setup --dry-run --all     # preview only
codex-images-mcp status
codex-images-mcp uninstall --all
```

Clients: `claude-code`, `claude-desktop`, `codex`, `cursor` and `opencode`. `setup` registers an MCP server named `codex-images` that runs `uvx --from codex-images-mcp codex-images-mcp-server`, and installs a `codex-images` skill for clients that support skills.

If a client already has a `codex-imagen` entry from the original project, `setup` leaves it untouched and tells you how to remove it. `uninstall` only ever removes `codex-images`.
