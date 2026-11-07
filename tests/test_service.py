import hashlib
import json
import time

import pytest
from PIL import Image

from codex_images_mcp._service import ServiceError, output_root, parse_output, postprocess, read_artifact


@pytest.mark.parametrize(("policy", "expected"), [
    ("native", (100, 50)), ("exact", (40, 40)), ("fit", (40, 20)), ("cover", (40, 40)),
])
def test_size_policies(tmp_path, policy, expected):
    source, target = tmp_path / "source.png", tmp_path / "target.png"
    Image.new("RGBA", (100, 50), (10, 20, 30, 128)).save(source)
    _, output = parse_output({"output": {"width": 40, "height": 40, "size_policy": policy}})
    assert postprocess(source, target, output) == (*expected, "image/png")
    with Image.open(target) as image:
        assert image.size == expected


def test_jpeg_flattens_alpha_on_white(tmp_path):
    source, target = tmp_path / "source.png", tmp_path / "target.jpeg"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(source)
    _, output = parse_output({"output": {"format": "jpeg"}})
    assert postprocess(source, target, output) == (10, 10, "image/jpeg")
    with Image.open(target) as image:
        assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


@pytest.mark.parametrize("output", [
    {"width": 0, "height": 10}, {"width": True, "height": 10},
    {"width": 10}, {"size_policy": "cover"}, {"width": 8193, "height": 1},
])
def test_invalid_dimensions(output):
    with pytest.raises(ServiceError) as error:
        parse_output({"output": output})
    assert error.value.code == "UNSUPPORTED_DIMENSIONS"


def test_root_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path / "env"))
    assert output_root() == tmp_path / "env"
    assert output_root(tmp_path / "explicit") == tmp_path / "explicit"


def test_artifact_lookup_integrity_expiry_and_confinement(tmp_path):
    job = tmp_path / "jobs" / ("a" * 32)
    job.mkdir(parents=True)
    path = job / "test.png"
    Image.new("RGB", (24, 12), "blue").save(path)
    data = path.read_bytes()
    metadata = {"created_ms": int(time.time() * 1000), "artifacts": [{"index": 0, "filename": path.name,
                "mime_type": "image/png", "sha256": hashlib.sha256(data).hexdigest()}]}
    metadata_path = job / "metadata.json"
    metadata_path.write_text(json.dumps(metadata))
    uri = f'mcp://codex-images/jobs/{"a" * 32}/artifacts/0'
    contents, record, remaining = read_artifact(tmp_path, uri)
    assert contents == data and record["sha256"] == hashlib.sha256(data).hexdigest()
    assert (record["width"], record["height"]) == (24, 12) and remaining > 0
    with pytest.raises(ServiceError):
        read_artifact(tmp_path, "mcp://codex-images/jobs/../artifacts/0")
    metadata["artifacts"][0]["filename"] = "../../outside.png"
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ServiceError):
        read_artifact(tmp_path, uri)
    metadata["artifacts"][0]["filename"] = path.name
    metadata["created_ms"] = 0
    metadata_path.write_text(json.dumps(metadata))
    with pytest.raises(ServiceError):
        read_artifact(tmp_path, uri)
    metadata["created_ms"] = int(time.time() * 1000)
    metadata_path.write_text(json.dumps(metadata))
    path.write_bytes(b"corrupt")
    with pytest.raises(ServiceError) as error:
        read_artifact(tmp_path, uri)
    assert error.value.code == "ARTIFACT_INCOMPLETE"


# --- generate / get_artifact -------------------------------------------------

import base64
from io import BytesIO

from codex_images_mcp import _service


def _png_bytes(size=(8, 8), color=(255, 0, 0, 255)):
    buffer = BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _fake_engine(calls, *, size=(8, 8), fail_index=None):
    def engine(args):
        calls.append(args)
        out = _service.Path(args["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        count = len(args["prompt"]) if isinstance(args["prompt"], list) else 1
        images, errors = [], []
        for index in range(count):
            if index == fail_index:
                errors.append({"code": "UPSTREAM_GENERATION_FAILED", "message": "boom", "layer": "provider",
                               "retryable": True, "input_index": index})
                continue
            path = out / f"raw-{index}.png"
            path.write_bytes(_png_bytes(size))
            images.append({"index": index, "path": str(path)})
        return {"ok": not errors, "images": images, "errors": errors, "warnings": [],
                "models": {"dispatched": "gpt-image-2"}}
    return engine


def test_generate_builds_v2_result_with_verified_artifact(tmp_path):
    calls = []
    result = _service.generate({"prompt": "a red square"}, _fake_engine(calls), output_dir=tmp_path)
    structured = result["structured"]
    assert result["is_error"] is False
    assert structured["schema_version"] == "codex-images.result/v2"
    assert structured["status"] == "completed"
    assert structured["batch"] == {"requested": 1, "completed": 1, "failed": 0, "recoverable": 1}
    artifact = structured["artifacts"][0]
    assert artifact["uri"] == f"mcp://codex-images/jobs/{structured['job_id']}/artifacts/0"
    assert (artifact["width"], artifact["height"], artifact["mime_type"]) == (8, 8, "image/png")
    stored = _service.Path(result["artifacts"][0]["path"])
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == artifact["sha256"]
    assert stored.is_relative_to(tmp_path)
    assert result["summary"].startswith("1 of 1 images generated successfully.")


def test_generate_warns_and_drops_deprecated_fields(tmp_path):
    calls = []
    result = _service.generate({"prompt": "x", "mode": "raw", "skills": ["a"]}, _fake_engine(calls), output_dir=tmp_path)
    assert "mode" not in calls[0] and "skills" not in calls[0]
    warnings = result["structured"]["warnings"]
    assert any(w.startswith("mode is ignored") for w in warnings)
    assert any(w.startswith("skills is ignored") for w in warnings)


def test_generate_partial_batch_reports_failed_item(tmp_path):
    result = _service.generate({"prompt": ["a", "b"]}, _fake_engine([], fail_index=1), output_dir=tmp_path)
    structured = result["structured"]
    assert result["is_error"] is True
    assert structured["status"] == "partial"
    assert structured["batch"] == {"requested": 2, "completed": 1, "failed": 1, "recoverable": 1}
    assert structured["artifacts"][1]["status"] == "failed"
    assert result["summary"].startswith("1 of 2 images generated; 1 failed.")


@pytest.mark.parametrize(("arguments", "code"), [
    ({"prompt": []}, "INVALID_ARGUMENT"),
    ({"prompt": "x", "references": ["/etc/passwd"]}, "LOCAL_PATH_FORBIDDEN"),
    ({"prompt": "x", "references": [{"source": {"type": "local_file", "path": "/tmp/a.png"}}]}, "LOCAL_PATH_FORBIDDEN"),
    ({"prompt": "x", "references": [{"source": {"type": "inline_blob", "data": base64.b64encode(b"not an image").decode()}}]}, "UNSUPPORTED_MIME_TYPE"),
    ({"prompt": "x", "client_destination": {"path": "/tmp"}}, "CLIENT_WRITE_FAILED"),
])
def test_generate_rejects_bad_input_without_calling_engine_or_leaving_job_dir(tmp_path, arguments, code):
    calls = []
    result = _service.generate(arguments, _fake_engine(calls), output_dir=tmp_path)
    assert result["is_error"] is True
    assert result["structured"]["errors"][0]["code"] == code
    assert calls == []
    assert not (tmp_path / "jobs" / result["structured"]["job_id"]).exists()


def test_generate_materializes_inline_reference(tmp_path):
    calls = []
    ref = {"source": {"type": "inline_blob", "data": base64.b64encode(_png_bytes()).decode()}, "mime_type": "image/png"}
    _service.generate({"prompt": "edit", "references": [ref]}, _fake_engine(calls), output_dir=tmp_path)
    path = _service.Path(calls[0]["references"][0])
    assert path.is_relative_to(tmp_path) and path.read_bytes() == _png_bytes()


def test_engine_exception_becomes_failed_result(tmp_path):
    def broken(_args):
        raise RuntimeError("secret detail")
    result = _service.generate({"prompt": "x"}, broken, output_dir=tmp_path)
    assert result["structured"]["status"] == "failed"
    assert "secret detail" not in json.dumps(result)


def test_get_artifact_inline_round_trip(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_IMAGES_PUBLIC_BASE_URL", raising=False)
    generated = _service.generate({"prompt": "x"}, _fake_engine([]), output_dir=tmp_path)
    uri = generated["structured"]["artifacts"][0]["uri"]
    result = _service.get_artifact({"uri": uri}, output_dir=tmp_path)
    assert result["is_error"] is False
    assert result["metadata"]["schema_version"] == "codex-images.artifact/v1"
    assert result["metadata"]["delivery"] == "inline"
    assert hashlib.sha256(result["data"]).hexdigest() == result["metadata"]["sha256"]


def test_get_artifact_download_requires_public_base_url(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_IMAGES_PUBLIC_BASE_URL", raising=False)
    generated = _service.generate({"prompt": "x"}, _fake_engine([]), output_dir=tmp_path)
    uri = generated["structured"]["artifacts"][0]["uri"]
    result = _service.get_artifact({"uri": uri, "delivery": "download"}, output_dir=tmp_path)
    assert result["is_error"] is True
    assert result["structured"]["errors"][0]["code"] == "DOWNLOAD_UNAVAILABLE"


def test_signed_download_is_single_use(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_PUBLIC_BASE_URL", "https://images.example.com")
    generated = _service.generate({"prompt": "x"}, _fake_engine([]), output_dir=tmp_path)
    uri = generated["structured"]["artifacts"][0]["uri"]
    result = _service.get_artifact({"uri": uri, "delivery": "download"}, output_dir=tmp_path)
    url = result["metadata"]["download"]["url"]
    assert url.startswith("https://images.example.com/download/")
    token = url.rsplit("/", 1)[1]
    assert _service.redeem_download(token) == uri
    with pytest.raises(ServiceError):
        _service.redeem_download(token)
    with pytest.raises(ServiceError):
        _service.redeem_download(token.split(".")[0] + ".deadbeef")


@pytest.mark.parametrize("arguments", [{"uri": "mcp://codex-images/jobs/../artifacts/0"}, {"uri": 1}, {"uri": "x", "extra": 1}])
def test_get_artifact_rejects_bad_arguments(tmp_path, arguments):
    result = _service.get_artifact(arguments, output_dir=tmp_path)
    assert result["is_error"] is True and result["data"] is None
