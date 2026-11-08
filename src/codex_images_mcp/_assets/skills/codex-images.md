---
name: codex-images
description: Primary image-generation tool when the codex-images MCP server is available — use codex_images for image creation and editing on the user's ChatGPT plan (gpt-image-2 via Codex Images, no OPENAI_API_KEY). Supports batches (single, parallel, variants, chain, branded-parallel), reference images and masks, native transparent PNGs, exact output sizes, and returns verifiable artifacts (SHA-256, MIME, dimensions). Prefer it over the built-in image tool when you need more than one image, consistency across images, transparency, an edit with a mask, or a precise output size.
---

# codex-images — caller reference

## Tools

- `codex_images` — generate or edit images. Returns `codex-images.result/v2`.
- `codex_images_artifact` — fetch one artifact by its `mcp://codex-images/...` URI: verified metadata plus the image inline, or a short-lived signed download URL (`delivery="download"`, only when the server has a public HTTPS base URL).

## Prompts

Prompts are sent **verbatim** to the image endpoint; there is no rewriting layer. Write the final prompt yourself.

- **String**: free text.
- **Labeled spec** (object): keys such as `primary_request`, `subject`, `scene`, `style_medium`, `composition`, `lighting_mood`, `palette`, `materials`, `text_verbatim`, `constraints`, `avoid` become deterministic `Label: value` lines.
- **List**: one request per item, run concurrently (up to four at a time).

## Batch modes (`batch_mode`)

| Mode | Use it for |
|---|---|
| `single` | One image (default for a single prompt). |
| `parallel` | Independent images from a list of prompts. |
| `variants` | `count` variations of one prompt. |
| `chain` | Each image references the anchor and the previous output — sequences, storyboards. |
| `branded-parallel` | Every image references a generated `anchor` — consistent brand sets, website sections. |

## Inputs

- `references`: up to five PNG/JPEG/WebP images per request. Forms: an HTTPS URL string (public hosts only), or `{"source": {"type": "inline_blob", "data": "<base64>"}, "role": "content"}`, or `{"source": {"type": "resource", "uri": "mcp://codex-images/..."}}` to reuse a previous artifact. Roles: `identity`, `style`, `composition`, `content`, `mask`.
- `mask`: an edit mask in the same forms; requires a reference. The backend treats masks as a loose hint and may regenerate the whole frame.
- Local file paths are rejected by the server. Clients that run on the same machine (CLI, Pi extension) read the file and send it as `inline_blob`.

## Output

- `transparent: true` asks for a native alpha channel; a warning is returned if the result is fully opaque. JPEG cannot hold alpha and is flattened onto white.
- `output`: `{"format": "png"|"jpeg"|"webp", "width": W, "height": H, "size_policy": "native"|"exact"|"fit"|"cover"}`. The backend chooses its own size; `exact`/`cover` give you exactly W×H, `fit` keeps the aspect ratio inside W×H, `native` keeps what the backend returned.
- `size` is only a hint to the backend; use `output` for final dimensions.

## Results

`status` is `completed`, `partial` or `failed`; `batch` counts requested/completed/failed items; each artifact has `uri`, `mime_type`, `bytes`, `width`, `height`, `sha256` (and `path` over stdio). Errors carry a `code` such as `LOCAL_PATH_FORBIDDEN`, `UNSUPPORTED_MIME_TYPE`, `UPSTREAM_AUTH_FAILED` or `UPSTREAM_GENERATION_FAILED`.

## Deprecated fields

`mode`, `skills`, `instruction_files`, `extra_instructions`, `enhance_prompt`, `image_model`, `output_dir` and every `chroma_*` field come from codex-imagen. They are accepted and ignored with a warning.
