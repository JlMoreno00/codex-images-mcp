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
