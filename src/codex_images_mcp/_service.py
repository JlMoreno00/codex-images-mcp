"""Transport-independent codex_images service.

Validates tool input, materializes references, calls the native engine,
post-processes and stores artifacts, and builds ``codex-images.result/v2``
results. Transports (stdio, HTTP) only convert the returned dictionaries into
MCP results. Importing this module does not create directories or contact a
provider.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import time
import uuid
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from PIL import Image, ImageOps

from ._native import DEPRECATED, RemoteImage
from ._schema import TOOL_INPUT_SCHEMA
from ._urls import RemoteURLValidationError, validate_public_https

MIME_BY_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpeg", "image/webp": ".webp"}
RESOURCE_RE = re.compile(r"^mcp://codex-images/jobs/([0-9a-f]{32})/artifacts/([0-9]+)$")
ARTIFACT_TTL_MS = 900_000
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
RESULT_SCHEMA = "codex-images.result/v2"
ARTIFACT_SCHEMA = "codex-images.artifact/v1"
VALID_ROLES = {"identity", "style", "composition", "content", "mask"}
MAX_INLINE_INPUT_BYTES = int(os.environ.get("CODEX_IMAGES_MAX_INLINE_INPUT_BYTES", str(8 * 1024 * 1024)))
MAX_REQUEST_INPUT_BYTES = int(os.environ.get("CODEX_IMAGES_MAX_REQUEST_INPUT_BYTES", str(24 * 1024 * 1024)))
# Our wire budget for inline image content; base64 adds ~33%.
MAX_INLINE_ARTIFACT_BYTES = 2 * 1024 * 1024
DOWNLOAD_TTL_SECONDS = min(300, max(1, int(os.environ.get("CODEX_IMAGES_DOWNLOAD_TTL_SECONDS", "120"))))
_DOWNLOAD_KEY = secrets.token_bytes(32)  # Restart revokes all outstanding links.
_DOWNLOAD_GRANTS: dict[str, tuple[str, float]] = {}


class ServiceError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def output_root(output_dir: str | Path | None = None) -> Path:
    """Resolve the configured store without creating it."""
    configured = output_dir or os.environ.get("CODEX_IMAGES_OUTPUT_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    if sys.platform == "darwin":
        return (Path.home() / "Library/Application Support/codex-images").resolve()
    return (Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "codex-images").expanduser().resolve()


def parse_output(arguments: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return engine arguments and validated output options without mutating input."""
    args = dict(arguments)
    output = args.pop("output", None)
    if output is None:
        output = {}
    if not isinstance(output, dict) or set(output) - {"format", "width", "height", "size_policy", "delivery"}:
        raise ServiceError("INVALID_ARGUMENT", "Invalid output options.")
    policy = output.get("size_policy", "native")
    if policy not in {"native", "exact", "fit", "cover"}:
        raise ServiceError("UNSUPPORTED_DIMENSIONS", f"Unknown size_policy {policy!r}.")
    fmt = output.get("format", args.get("output_format", "png"))
    if fmt not in {"png", "jpeg", "webp"}:
        raise ServiceError("UNSUPPORTED_MIME_TYPE", f"Unsupported output format {fmt!r}.")
    width, height = output.get("width"), output.get("height")
    if (width is None) != (height is None):
        raise ServiceError("UNSUPPORTED_DIMENSIONS", "width and height must be supplied together.")
    if width is not None and any(type(value) is not int or not 1 <= value <= 8192 for value in (width, height)):
        raise ServiceError("UNSUPPORTED_DIMENSIONS", "Output dimensions must be positive integers no larger than 8192.")
    if policy != "native" and width is None:
        raise ServiceError("UNSUPPORTED_DIMENSIONS", f"size_policy={policy!r} requires width and height.")
    delivery = output.get("delivery", "resource_links")
    if delivery != "resource_links":
        raise ServiceError("INVALID_ARGUMENT", "Unsupported output delivery.")
    args["output_format"] = fmt
    if width is not None:
        args["size"] = f"{width}x{height}"
    return args, {"format": fmt, "width": width, "height": height, "size_policy": policy, "delivery": delivery}


def postprocess(source: Path, target: Path, output: dict[str, Any]) -> tuple[int, int, str]:
    """Convert an engine image, returning dimensions verified from stored bytes."""
    fmt = output["format"]
    save_format = {"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}[fmt]
    width, height, policy = output["width"], output["height"], output["size_policy"]
    try:
        with Image.open(source) as image:
            image.load()
            if width is not None and policy == "exact":
                image = image.resize((width, height), Image.Resampling.LANCZOS)
            elif width is not None and policy == "fit":
                image.thumbnail((width, height), Image.Resampling.LANCZOS)
            elif width is not None and policy == "cover":
                image = ImageOps.fit(image, (width, height), method=Image.Resampling.LANCZOS)
            if save_format == "JPEG" and image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, "white")
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            target.parent.mkdir(parents=True, exist_ok=True)
            image.save(target, format=save_format)
        with Image.open(target) as image:
            image.load()
            actual, actual_format = image.size, image.format
        if actual_format != save_format or (policy in {"exact", "cover"} and actual != (width, height)):
            raise ValueError("Unexpected output format or dimensions")
        return actual[0], actual[1], MIME_BY_FORMAT[save_format]
    except (OSError, ValueError) as exc:
        raise ServiceError("ARTIFACT_INCOMPLETE", "Generated artifact is not a readable image.", retryable=True) from exc


def read_artifact(root: str | Path, uri: str, *, ttl_ms: int = ARTIFACT_TTL_MS) -> tuple[bytes, dict[str, Any], int]:
    """Read a confined, unexpired artifact and verify its integrity and MIME."""
    match = RESOURCE_RE.fullmatch(uri) if isinstance(uri, str) else None
    if not match:
        raise ServiceError("ARTIFACT_READ_FAILED", "Unknown or malformed artifact resource URI.")
    root = Path(root).resolve()
    jobs = (root / "jobs").resolve()
    job = (jobs / match.group(1)).resolve()
    try:
        jobs.relative_to(root)
        job.relative_to(jobs)
        metadata = json.loads((job / "metadata.json").read_text("utf-8"))
        remaining = ttl_ms - (int(time.time() * 1000) - int(metadata["created_ms"]))
        if remaining <= 0:
            raise ServiceError("ARTIFACT_READ_FAILED", "Artifact resource has expired.")
        record = next(item for item in metadata["artifacts"] if item["index"] == int(match.group(2)))
        path = (job / record["filename"]).resolve()
        path.relative_to(job)
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            raise ServiceError("ARTIFACT_TOO_LARGE", "Artifact exceeds the server retrieval budget.")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != record["sha256"]:
            raise ServiceError("ARTIFACT_INCOMPLETE", "Artifact integrity check failed.")
        with Image.open(BytesIO(data)) as image:
            mime, dimensions = MIME_BY_FORMAT.get(image.format or ""), image.size
            image.verify()
        if mime != record["mime_type"] or mime not in EXT_BY_MIME or path.suffix.lower() != EXT_BY_MIME[mime]:
            raise ServiceError("ARTIFACT_INCOMPLETE", "Image MIME or filename extension is inconsistent.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", path.name):
            raise ServiceError("ARTIFACT_INCOMPLETE", "Unsafe artifact filename.")
        return data, {"uri": uri, "filename": path.name, "extension": path.suffix.lower(), "mime_type": mime,
                      "width": dimensions[0], "height": dimensions[1], "bytes": len(data), "sha256": digest}, remaining
    except ServiceError:
        raise
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        raise ServiceError("ARTIFACT_READ_FAILED", "Artifact metadata or bytes are missing or unreadable.") from exc


V2_INPUT_SCHEMA = deepcopy(TOOL_INPUT_SCHEMA)
V2_INPUT_SCHEMA["properties"]["references"]["items"] = {
    "oneOf": [
        {"type": "string"},
        {
            "type": "object",
            "required": ["source"],
            "properties": {
                "source": {"type": "object", "required": ["type"], "additionalProperties": True},
                "role": {"oneOf": [{"type": "string", "enum": sorted(VALID_ROLES)},
                                   {"type": "array", "items": {"type": "string", "enum": sorted(VALID_ROLES)}}]},
                "mime_type": {"type": "string"},
                "sha256": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ]
}
V2_INPUT_SCHEMA["properties"]["output"] = {
    "type": "object",
    "properties": {
        "format": {"type": "string", "enum": ["png", "jpeg", "webp"]},
        "width": {"type": "integer", "minimum": 1, "maximum": 8192},
        "height": {"type": "integer", "minimum": 1, "maximum": 8192},
        "size_policy": {"type": "string", "enum": ["cover", "exact", "fit", "native"]},
        "delivery": {"type": "string", "enum": ["resource_links"]},
    },
    "additionalProperties": False,
}
V2_INPUT_SCHEMA["properties"]["client_destination"] = {"type": "object"}

ARTIFACT_INPUT_SCHEMA = {
    "type": "object", "required": ["uri"], "additionalProperties": False,
    "properties": {
        "uri": {"type": "string", "maxLength": 256},
        "delivery": {"type": "string", "enum": ["auto", "inline", "download"], "default": "auto"},
    },
}


def typed_error(code: str, message: str, *, layer: str = "server", retryable: bool = False, **extra: Any) -> dict[str, Any]:
    return {"code": code, "layer": layer, "retryable": retryable, "message": message, **extra}


def failure(error: dict[str, Any], *, job_id: str | None = None, warnings: list[str] | None = None, requested: int = 0) -> dict[str, Any]:
    """Service result for a request that produced no artifacts."""
    structured = {
        "schema_version": RESULT_SCHEMA,
        "status": "failed",
        "job_id": job_id or uuid.uuid4().hex,
        "batch": {"requested": requested, "completed": 0, "failed": requested, "recoverable": 0},
        "artifacts": [],
        "errors": [error],
        "warnings": warnings or [],
    }
    return {"is_error": True, "summary": error["message"], "structured": structured, "artifacts": []}


def _classify_upstream(message: str) -> dict[str, Any]:
    lower = message.lower()
    if any(marker in lower for marker in ("401", "403", "unauthorized", "auth", "credential", "token")):
        return typed_error("UPSTREAM_AUTH_FAILED", message, layer="provider")
    return typed_error("UPSTREAM_GENERATION_FAILED", message, layer="provider", retryable=True)


def _requested_count(arguments: dict[str, Any]) -> int:
    prompt = arguments.get("prompt")
    if isinstance(prompt, list):
        return len(prompt)
    if arguments.get("batch_mode") == "variants":
        return max(1, int(arguments.get("count", 1)))
    return 1


def _public_https(url: Any) -> str:
    if not isinstance(url, str):
        raise ServiceError("REMOTE_DOWNLOAD_FAILED", "Remote inputs must use an HTTPS URL.")
    try:
        return validate_public_https(url)[0]
    except RemoteURLValidationError as exc:
        raise ServiceError(exc.code, str(exc), retryable=exc.retryable) from exc


def _materialize_inline(source: dict[str, Any], input_dir: Path, index: int) -> Path:
    encoded = source.get("data") or source.get("blob")
    if not isinstance(encoded, str):
        raise ServiceError("INPUT_MATERIALIZATION_FAILED", "inline_blob requires Base64 data.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ServiceError("INPUT_MATERIALIZATION_FAILED", "inline_blob is not valid Base64.") from exc
    if len(data) > MAX_INLINE_INPUT_BYTES:
        raise ServiceError("INPUT_MATERIALIZATION_FAILED", "inline_blob exceeds the per-file size limit.")
    return _store_image(data, source.get("mime_type") or "application/octet-stream", input_dir, index)


def _store_image(data: bytes, mime: str, input_dir: Path, index: int) -> Path:
    try:
        with Image.open(BytesIO(data)) as image:
            detected = MIME_BY_FORMAT.get(image.format or "")
            image.verify()
    except OSError as exc:
        raise ServiceError("UNSUPPORTED_MIME_TYPE", "Inline reference is not a supported image.") from exc
    if detected not in EXT_BY_MIME:
        raise ServiceError("UNSUPPORTED_MIME_TYPE", "Inline reference is not PNG, JPEG, or WebP.")
    if mime not in {detected, "application/octet-stream"}:
        raise ServiceError("UNSUPPORTED_MIME_TYPE", "Declared MIME type does not match input content.")
    input_dir.mkdir(parents=True, exist_ok=True)
    path = input_dir / f"input-{index}{EXT_BY_MIME[detected]}"
    path.write_bytes(data)
    return path


def _prepare_inputs(arguments: dict[str, Any], root: Path, job_dir: Path, warnings: list[str]) -> dict[str, Any]:
    args = dict(arguments)
    refs: list[Any] = []
    mask_refs: list[Any] = []
    total_inline = 0
    for index, item in enumerate(args.get("references") or []):
        if isinstance(item, str):
            if item.startswith("https://"):
                refs.append(RemoteImage(item, _public_https(item)))
                continue
            raise ServiceError("LOCAL_PATH_FORBIDDEN", "String references must be HTTPS URLs; send local files as inline_blob.")
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            raise ServiceError("INPUT_MATERIALIZATION_FAILED", f"Reference {index} has an invalid source.")
        roles = item.get("role", "content")
        roles = [roles] if isinstance(roles, str) else roles
        if not roles or any(role not in VALID_ROLES for role in roles):
            raise ServiceError("INPUT_MATERIALIZATION_FAILED", f"Reference {index} has an unsupported role.")
        target = mask_refs if "mask" in roles else refs
        source = item["source"]
        kind = source.get("type")
        if kind == "local_file":
            raise ServiceError("LOCAL_PATH_FORBIDDEN", "Client-local paths must be materialized by the MCP adapter before calling the server.")
        if kind == "url":
            url = source.get("url") or source.get("uri")
            target.append(RemoteImage(url, _public_https(url)))
        elif kind == "inline_blob":
            path = _materialize_inline({**source, "mime_type": item.get("mime_type") or source.get("mime_type")}, job_dir / "inputs", index)
            total_inline += path.stat().st_size
            target.append(str(path))
        elif kind == "resource":
            data, meta, _remaining = read_artifact(root, str(source.get("uri")))
            target.append(str(_store_image(data, meta["mime_type"], job_dir / "inputs", index)))
        else:
            raise ServiceError("INPUT_MATERIALIZATION_FAILED", f"Unsupported reference source type: {kind!r}.")
    if total_inline > MAX_REQUEST_INPUT_BYTES:
        raise ServiceError("INPUT_MATERIALIZATION_FAILED", "Inline inputs exceed the per-request size limit.")
    args["references"] = refs

    for field in DEPRECATED:
        if field in args:
            warnings.append(f"{field} is ignored (deprecated): prompts are sent verbatim to the image endpoint.")
            args.pop(field)
    mask = args.get("mask")
    if len(mask_refs) > 1 or (mask_refs and mask is not None):
        raise ServiceError("INPUT_MATERIALIZATION_FAILED", "Supply exactly one mask, either top-level or as a mask-role reference.")
    if mask_refs:
        args["mask"] = mask_refs[0]
    elif mask:
        if isinstance(mask, dict):
            mask = {**mask, "role": "content"}
        args["mask"] = _prepare_inputs({"references": [mask]}, root, job_dir / "mask", warnings)["references"][0]
    if args.get("mask") and not refs:
        raise ServiceError("INPUT_MATERIALIZATION_FAILED", "A mask requires an ordinary reference image to edit.")
    if "client_destination" in args:
        raise ServiceError("CLIENT_WRITE_FAILED", "client_destination is adapter-only and must not be sent to the server.")
    return args


def generate(arguments: dict[str, Any], engine: Callable[[dict[str, Any]], dict[str, Any]], *, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Run one codex_images call and return a transport-neutral result."""
    root = output_root(output_dir)
    job_id = uuid.uuid4().hex
    job_dir = (root / "jobs" / job_id).resolve()
    warnings: list[str] = []
    requested = _requested_count(arguments)
    try:
        if arguments.get("prompt") == []:
            raise ServiceError("INVALID_ARGUMENT", "Prompt list must contain at least one item.")
        args = _prepare_inputs(arguments, root, job_dir, warnings)
        args, output = parse_output(args)
    except ServiceError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        error = typed_error(exc.code, str(exc), retryable=exc.retryable)
        return failure(error, job_id=job_id, warnings=warnings, requested=requested)

    args["output_dir"] = str(job_dir / "generated")
    try:
        raw = engine(args)
    except Exception:  # defensive boundary: never leak an engine exception
        raw = {"ok": False, "images": [], "warnings": [], "error": "Image generation failed."}
    warnings.extend(str(item) for item in raw.get("warnings") or [])
    if args.get("transparent") and output["format"] == "jpeg":
        warnings.append("JPEG does not support transparency; alpha is flattened onto white.")

    images = {int(item.get("index", pos)): item for pos, item in enumerate(raw.get("images") or [])}
    records: list[dict[str, Any]] = []
    stored: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    total_failure = not raw.get("ok") and not images
    if total_failure:
        errors.extend(raw.get("errors") or [_classify_upstream(str(raw.get("error") or "Image generation failed."))])
    for index in range(requested):
        image = images.get(index)
        if not image:
            if total_failure:
                continue
            error = next((item for item in raw.get("errors") or [] if item.get("input_index") == index), None) or typed_error(
                "UPSTREAM_GENERATION_FAILED", f"Generation failed for batch item {index}.", layer="provider", retryable=True, input_index=index)
            records.append({"index": index, "status": "failed", "error": error})
            errors.append(error)
            continue
        try:
            filename = f"{index:02d}-{uuid.uuid4().hex[:12]}{EXT_BY_MIME['image/' + output['format']]}"
            target = job_dir / filename
            width, height, mime = postprocess(Path(str(image.get("path", ""))), target, output)
            data = target.read_bytes()
            record = {
                "index": index,
                "status": "completed",
                "artifact_id": f"artifact_{index}",
                "uri": f"mcp://codex-images/jobs/{job_id}/artifacts/{index}",
                "name": filename,
                "mime_type": mime,
                "bytes": len(data),
                "width": width,
                "height": height,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            records.append(record)
            stored.append({**record, "filename": filename, "path": str(target)})
        except (OSError, ServiceError) as exc:
            code = exc.code if isinstance(exc, ServiceError) else "ARTIFACT_PERSISTENCE_FAILED"
            error = typed_error(code, str(exc), retryable=True, input_index=index)
            records.append({"index": index, "status": "failed", "error": error})
            errors.append(error)

    completed = len(stored)
    failed = requested - completed
    status = "completed" if failed == 0 else ("partial" if completed else "failed")
    if not raw.get("ok") and not errors:
        errors.extend(raw.get("errors") or [_classify_upstream(str(raw.get("error") or "Image generation failed."))])
    structured: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "status": status,
        "job_id": job_id,
        "batch": {"requested": requested, "completed": completed, "failed": failed, "recoverable": completed},
        "artifacts": records,
        "manifest_uri": f"mcp://codex-images/jobs/{job_id}/manifest/manifest",
        "warnings": warnings,
        "errors": errors,
        "models": raw.get("models") or {},
    }
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manifest.json").write_text(json.dumps(structured, indent=2, ensure_ascii=False), "utf-8")
    (job_dir / "metadata.json").write_text(json.dumps({"created_ms": int(time.time() * 1000), "artifacts": stored}), "utf-8")

    if status == "completed":
        summary = f"{completed} of {requested} images generated successfully."
    elif status == "partial":
        summary = f"{completed} of {requested} images generated; {failed} failed."
    else:
        summary = str(raw.get("error") or "Image generation failed.")
    if structured["models"].get("dispatched"):
        summary += f" image_model={structured['models']['dispatched']} (server-managed, unconfirmed)."
    return {"is_error": status != "completed", "summary": summary, "structured": structured, "artifacts": stored}


def public_base_url() -> str | None:
    """Configured HTTPS base for signed downloads, or None when downloads are off."""
    value = os.environ.get("CODEX_IMAGES_PUBLIC_BASE_URL", "").rstrip("/")
    return value or None


def issue_download(uri: str, remaining_ms: int) -> dict[str, Any]:
    base = public_base_url()
    parsed = urlparse(base or "")
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ServiceError("DOWNLOAD_UNAVAILABLE", "Downloads need CODEX_IMAGES_PUBLIC_BASE_URL set to an HTTPS URL.")
    now = time.time()
    for key, (_, expires) in list(_DOWNLOAD_GRANTS.items()):
        if expires <= now:
            del _DOWNLOAD_GRANTS[key]
    if len(_DOWNLOAD_GRANTS) >= 4096:
        raise ServiceError("DOWNLOAD_UNAVAILABLE", "Download grant capacity reached; retry later.", retryable=True)
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(_DOWNLOAD_KEY, nonce.encode(), hashlib.sha256).hexdigest()
    expires = now + min(DOWNLOAD_TTL_SECONDS, remaining_ms / 1000)
    _DOWNLOAD_GRANTS[nonce] = (uri, expires)
    return {"url": f"{base}/download/{nonce}.{signature}", "expires_at_ms": int(expires * 1000),
            "ttl_ms": int((expires - now) * 1000), "authorization": "opaque_signed_capability", "method": "GET"}


def redeem_download(token: str) -> str:
    """Return the artifact URI for a valid, unexpired download token (single use)."""
    nonce, _, signature = token.partition(".")
    expected = hmac.new(_DOWNLOAD_KEY, nonce.encode(), hashlib.sha256).hexdigest()
    grant = _DOWNLOAD_GRANTS.pop(nonce, None) if hmac.compare_digest(signature, expected) else None
    if not grant or grant[1] <= time.time():
        raise ServiceError("DOWNLOAD_UNAVAILABLE", "Download link is invalid or expired.")
    return grant[0]


def get_artifact(arguments: dict[str, Any], *, output_dir: str | Path | None = None) -> dict[str, Any]:
    """codex_images_artifact: return metadata plus inline bytes or a signed download."""
    try:
        uri = arguments.get("uri")
        delivery = arguments.get("delivery", "auto")
        if not isinstance(uri, str) or len(uri) > 256 or delivery not in {"auto", "inline", "download"} or set(arguments) - {"uri", "delivery"}:
            raise ServiceError("INVALID_ARGUMENT", "Invalid artifact retrieval arguments.")
        data, metadata, remaining = read_artifact(output_root(output_dir), uri)
        inline = delivery == "inline" or (delivery == "auto" and (len(data) <= MAX_INLINE_ARTIFACT_BYTES or public_base_url() is None))
        if inline and len(data) > MAX_INLINE_ARTIFACT_BYTES and delivery == "inline":
            raise ServiceError("INLINE_LIMIT_EXCEEDED", "Use delivery=download for this artifact.")
        metadata.update(schema_version=ARTIFACT_SCHEMA, delivery="inline" if inline else "download", ttl_ms=remaining)
        if not inline:
            metadata["download"] = issue_download(uri, remaining)
        return {"is_error": False, "metadata": metadata, "data": data if inline else None}
    except ServiceError as exc:
        error = typed_error(exc.code, "Artifact retrieval failed: " + exc.code, retryable=exc.retryable)
        return {**failure(error), "metadata": None, "data": None}
