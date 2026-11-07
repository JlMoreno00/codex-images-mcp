"""Transport-independent image post-processing and artifact primitives.

The service orchestration and input materialization will be added in the next
slice. Importing this module does not create directories or contact a provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

MIME_BY_FORMAT = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpeg", "image/webp": ".webp"}
RESOURCE_RE = re.compile(r"^mcp://codex-images/jobs/([0-9a-f]{32})/artifacts/([0-9]+)$")
ARTIFACT_TTL_MS = 900_000
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


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
