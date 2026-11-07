# MCP tools reference

The server is named `codex-images` and exposes two tools. The stdio server and the HTTP server behave the same way, with two differences: over stdio each artifact also carries its local `path`, and over HTTP, `codex_images_artifact` can issue a signed download URL.

## `codex_images`

Generate or edit images. The only required field is `prompt`.

| Field | Type | Meaning |
|---|---|---|
| `prompt` | string, object or array | String: sent verbatim. Object: a labeled spec (`primary_request`, `subject`, `scene`, `style_medium`, `composition`, `lighting_mood`, `palette`, `materials`, `text_verbatim`, `constraints`, `avoid`, …) rendered as `Label: value` lines. Array: one request per item, up to four at a time. |
| `batch_mode` | `auto`, `single`, `parallel`, `variants`, `chain`, `branded-parallel` | How several images relate. See below. |
| `count` | integer ≥ 1 | Number of images for `variants`. |
| `anchor` | string or object | Prompt for an anchor image that `chain` and `branded-parallel` reference. |
| `references` | array | Up to five reference images per request (the anchor and previous image count toward the five). |
| `mask` | string or object | Edit mask in the same forms as a reference. Requires a reference. |
| `transparent` | boolean | Request a native transparent background. |
| `output` | object | Final format and dimensions, see below. |
| `size` | string | Size hint for the backend only. Use `output` for final dimensions. |
| `output_format` | `png`, `jpeg`, `webp` | Shortcut for `output.format`. |

### Batch modes

| Mode | Behavior |
|---|---|
| `single` | One image. |
| `parallel` | Independent images from a list of prompts. |
| `variants` | `count` images from one prompt. |
| `chain` | Each image references the anchor and the previous output. |
| `branded-parallel` | Every image references the anchor; used for consistent sets. |
| `auto` | `branded-parallel` when an `anchor` is given, otherwise `parallel` or `single`. |

### References and masks

A reference is either an HTTPS URL string or an object:

```json
{"source": {"type": "inline_blob", "data": "<base64>"}, "mime_type": "image/png", "role": "content"}
{"source": {"type": "url", "url": "https://example.com/photo.jpg"}}
{"source": {"type": "resource", "uri": "mcp://codex-images/jobs/<job>/artifacts/0"}}
```

- `role` is one of `identity`, `style`, `composition`, `content` or `mask`, or a list of them. A reference with role `mask` is used as the mask.
- URLs must be HTTPS and resolve only to public addresses. The server connects to the address it checked and does not follow redirects.
- Inline images must be real PNG, JPEG or WebP. The type is detected from the bytes, and a declared `mime_type` must match. Limits: 8 MiB per file and 24 MiB per request.
- Local paths (`/home/...`, `{"type": "local_file"}`) are rejected with `LOCAL_PATH_FORBIDDEN`. Local clients (the CLI, the Pi extension) read the file and send it as `inline_blob`.
- The backend treats masks as a loose hint and may regenerate the whole frame.

### Output

```json
{"output": {"format": "png", "width": 1200, "height": 630, "size_policy": "cover"}}
```

| `size_policy` | Result |
|---|---|
| `native` (default) | Whatever size the backend returned. |
| `exact` | Resized to exactly `width`×`height` (aspect ratio may change). |
| `fit` | Scaled to fit inside `width`×`height`, aspect ratio kept. |
| `cover` | Scaled and center-cropped to exactly `width`×`height`. |

`exact`, `fit` and `cover` require both `width` and `height` (1–8192). JPEG cannot store alpha, so transparent pixels are flattened onto white, with a warning.

### Result: `codex-images.result/v2`

```json
{
  "schema_version": "codex-images.result/v2",
  "status": "completed",
  "job_id": "5f0c…",
  "batch": {"requested": 2, "completed": 2, "failed": 0, "recoverable": 2},
  "artifacts": [
    {"index": 0, "status": "completed", "artifact_id": "artifact_0",
     "uri": "mcp://codex-images/jobs/5f0c…/artifacts/0", "name": "00-1a2b3c4d5e6f.png",
     "mime_type": "image/png", "bytes": 482113, "width": 1024, "height": 1024,
     "sha256": "…"}
  ],
  "manifest_uri": "mcp://codex-images/jobs/5f0c…/manifest/manifest",
  "warnings": [],
  "errors": [],
  "models": {"dispatched": "gpt-image-2"}
}
```

- `status` is `completed`, `partial` (some images failed) or `failed`. Only `completed` is reported as a successful tool result.
- A failed batch item stays in `artifacts` as `{"index": n, "status": "failed", "error": {...}}`.
- Tool content holds a one-line summary plus one `resource_link` per completed image. Read a link with `resources/read` or with `codex_images_artifact`.
- Artifacts expire 15 minutes after the job was created.

## `codex_images_artifact`

| Field | Type | Meaning |
|---|---|---|
| `uri` | string | An `mcp://codex-images/jobs/<job>/artifacts/<n>` URI. |
| `delivery` | `auto`, `inline`, `download` | `auto` returns images up to 2 MiB inline. Larger images get a signed download when the server has a public base URL, and are returned inline otherwise. |

The result (`codex-images.artifact/v1`) carries `filename`, `mime_type`, `width`, `height`, `bytes`, `sha256` and `ttl_ms`. Images are re-verified from disk before they are returned. Downloads add `download.url`, a single-use HTTPS link valid for at most 120 seconds. Treat it like a password.

## Error codes

| Code | Meaning |
|---|---|
| `INVALID_ARGUMENT` | Malformed arguments (for example an empty prompt list). |
| `LOCAL_PATH_FORBIDDEN` | A local path was sent to the server; send the file inline. |
| `INPUT_MATERIALIZATION_FAILED` | A reference or mask could not be prepared (bad base64, too large, two masks, mask without reference). |
| `UNSUPPORTED_MIME_TYPE` | An input is not PNG, JPEG or WebP, or its declared type does not match. |
| `TOO_MANY_REFERENCES` | More than five references in one request. |
| `REMOTE_DOWNLOAD_FAILED` | A URL reference is not HTTPS, resolves to a private address, redirects or failed to download. |
| `UNSUPPORTED_DIMENSIONS` | Invalid `output` size or policy. |
| `CLIENT_WRITE_FAILED` | `client_destination` was sent to the server; it is for client adapters only. |
| `UPSTREAM_AUTH_FAILED` | The ChatGPT session is missing or expired. Run `codex-images-mcp login`. |
| `UPSTREAM_GENERATION_FAILED` | The image endpoint failed; usually retryable. |
| `ARTIFACT_INCOMPLETE`, `ARTIFACT_PERSISTENCE_FAILED` | A generated image could not be verified or stored. |
| `ARTIFACT_READ_FAILED`, `ARTIFACT_TOO_LARGE` | Unknown, expired or oversized artifact. |
| `INLINE_LIMIT_EXCEEDED` | `delivery=inline` was requested for an image over 2 MiB. |
| `DOWNLOAD_UNAVAILABLE` | Downloads are not configured, or the link is invalid or expired. |

Each error object has `code`, `message`, `layer` (`server` or `provider`), `retryable` and, for batch items, `input_index`.

## Deprecated fields

These fields come from codex-imagen. They are accepted and ignored, each with a warning: `mode`, `skills`, `instruction_files`, `extra_instructions`, `enhance_prompt`, `image_model`, `output_dir`, `vars`, `parallel`, `wall_clock_timeout`, `advanced` and every `chroma_*` field.
