"""Optional OpenAI API-key engine (CODEX_IMAGES_ENGINE=api). No network."""
import base64
import json
from email.message import Message
from io import BytesIO

import pytest
from PIL import Image

from codex_images_mcp import _native as engine

KEY = "sk-test-0123456789abcdef"


def _png():
    buf = BytesIO()
    Image.new("RGBA", (8, 8), (1, 2, 3, 0)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return BytesIO(json.dumps({"data": [{"b64_json": base64.b64encode(_png()).decode()}]}).encode())

    def no_oauth(_):
        raise AssertionError("the API engine must not use ChatGPT OAuth")

    monkeypatch.setattr(engine, "urlopen", opener)
    monkeypatch.setattr(engine, "oauth_headers", no_oauth)
    return seen


@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_ENGINE", "api")
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("CODEX_IMAGES_API_MODEL", raising=False)


def _body(request):
    return json.loads(request.data)


def test_api_engine_calls_public_images_api_with_default_model(tmp_path, api_env, calls):
    result = engine.generate({"prompt": "a mug", "transparent": True, "output_dir": str(tmp_path)})
    assert result["ok"] is True
    request = calls[0]
    assert request.full_url == "https://api.openai.com/v1/images/generations"
    assert request.get_header("Authorization") == f"Bearer {KEY}"
    assert request.get_header("Originator") is None
    body = _body(request)
    assert body["model"] == "gpt-image-2.6-flare"
    assert body["prompt"] == "a mug"
    assert body["background"] == "transparent"
    assert body["output_format"] == "png"
    assert result["models"] == {"engine": "openai-api", "requested": "gpt-image-2.6-flare",
                                "dispatched": "gpt-image-2.6-flare", "confirmed": True}


def test_api_engine_honors_model_quality_and_complexity(tmp_path, api_env, calls, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_API_MODEL", "gpt-image-2.5-flare")
    engine.generate({"prompt": "x", "model": "gpt-image-2.6-sunburst", "quality": "max",
                     "complexity": "low", "output_dir": str(tmp_path)})
    body = _body(calls[0])
    assert (body["model"], body["quality"], body["complexity"]) == ("gpt-image-2.6-sunburst", "max", "low")


def test_api_engine_edits_send_references_and_mask(tmp_path, api_env, calls):
    ref, mask = tmp_path / "ref.png", tmp_path / "mask.png"
    ref.write_bytes(_png())
    mask.write_bytes(_png())
    engine.generate({"prompt": "edit", "references": [str(ref)], "mask": str(mask), "output_dir": str(tmp_path)})
    request = calls[0]
    assert request.full_url == "https://api.openai.com/v1/images/edits"
    body = _body(request)
    assert body["images"][0]["image_url"].startswith("data:image/png;base64,")
    assert body["mask"]["image_url"].startswith("data:image/png;base64,")


def test_api_engine_without_key_fails_before_any_request(tmp_path, calls, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_ENGINE", "api")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = engine.generate({"prompt": "x", "output_dir": str(tmp_path)})
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "UPSTREAM_AUTH_FAILED"
    assert "OPENAI_API_KEY" in result["errors"][0]["message"]
    assert calls == []


def test_api_error_never_leaks_the_key(tmp_path, api_env, monkeypatch):
    def rejected(request, timeout):
        raise engine.HTTPError(request.full_url, 401, "Unauthorized", Message(),
                               BytesIO(json.dumps({"error": {"message": f"Incorrect API key {KEY}"}}).encode()))

    monkeypatch.setattr(engine, "urlopen", rejected)
    result = engine.generate({"prompt": "x", "output_dir": str(tmp_path)})
    assert result["errors"][0]["code"] == "UPSTREAM_AUTH_FAILED"
    assert KEY not in json.dumps(result)


@pytest.mark.parametrize(("field", "value"), [("quality", "ultra"), ("complexity", "max"), ("model", "dall-e-3; rm")])
def test_invalid_api_options_are_rejected(tmp_path, api_env, calls, field, value):
    result = engine.generate({"prompt": "x", field: value, "output_dir": str(tmp_path)})
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "INVALID_ARGUMENT"
    assert calls == []


def test_unknown_engine_is_rejected(tmp_path, calls, monkeypatch):
    monkeypatch.setenv("CODEX_IMAGES_ENGINE", "dalle")
    result = engine.generate({"prompt": "x", "output_dir": str(tmp_path)})
    assert result["errors"][0]["code"] == "INVALID_ARGUMENT"
    assert "CODEX_IMAGES_ENGINE" in result["errors"][0]["message"]
    assert calls == []


def test_plan_engine_ignores_api_options_with_a_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_IMAGES_ENGINE", raising=False)
    seen = []

    def opener(request, timeout):
        seen.append(request)
        return BytesIO(json.dumps({"data": [{"b64_json": base64.b64encode(_png()).decode()}]}).encode())

    monkeypatch.setattr(engine, "urlopen", opener)
    monkeypatch.setattr(engine, "oauth_headers", lambda _: {"Authorization": "Bearer oauth"})
    result = engine.generate({"prompt": "x", "model": "gpt-image-2.6-sunburst", "quality": "max",
                              "output_dir": str(tmp_path)})
    assert seen[0].full_url == engine.BASE + "/generations"
    assert _body(seen[0])["model"] == "gpt-image-2"
    assert any("model, quality are ignored on the ChatGPT plan route" in w for w in result["warnings"])
    assert result["models"]["engine"] == "chatgpt-plan"
    assert result["models"]["confirmed"] is False


def test_mcp_tool_forwards_api_options_and_reports_engine(tmp_path, api_env, calls, monkeypatch):
    from codex_images_mcp import mcp_server

    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path))
    result = mcp_server.call_tool("codex_images", {"prompt": "x", "model": "gpt-image-2.6-sunburst",
                                                   "quality": "high", "complexity": "medium"})
    assert result.isError is False
    body = _body(calls[0])
    assert (body["model"], body["quality"], body["complexity"]) == ("gpt-image-2.6-sunburst", "high", "medium")
    assert result.structuredContent is not None
    assert result.structuredContent["models"]["engine"] == "openai-api"
    assert KEY not in json.dumps(result.structuredContent)


def test_cli_generate_passes_api_options(tmp_path, api_env, calls, monkeypatch):
    from click.testing import CliRunner

    from codex_images_mcp import cli

    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path))
    result = CliRunner().invoke(cli._main_group, ["generate", "x", "--model", "gpt-image-2.6-sunburst",
                                                  "--quality", "max", "--complexity", "low"])
    assert result.exit_code == 0, result.output
    body = _body(calls[0])
    assert (body["model"], body["quality"], body["complexity"]) == ("gpt-image-2.6-sunburst", "max", "low")
    assert json.loads(result.stdout)["models"]["dispatched"] == "gpt-image-2.6-sunburst"
