"""Tests for the ``codex-images-mcp`` CLI (generate + installer subcommands)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from codex_images_mcp import cli as cli_mod


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    calls: list[dict] = []

    def engine(args):
        calls.append(args)
        out = Path(args["output_dir"])
        out.mkdir(parents=True, exist_ok=True)
        count = args.get("count", 1) if args.get("batch_mode") == "variants" else 1
        images = []
        for index in range(count):
            path = out / f"raw-{index}.png"
            Image.new("RGBA", (10, 6), (1, 2, 3, 255)).save(path)
            images.append({"index": index, "path": str(path)})
        return {"ok": True, "images": images, "warnings": [], "models": {}}

    monkeypatch.setattr(cli_mod._native, "generate", engine)
    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path / "store"))
    return calls


def _png(path: Path) -> Path:
    Image.new("RGBA", (4, 4), (0, 0, 0, 255)).save(path)
    return path


def test_help_lists_subcommands(runner: CliRunner) -> None:
    result = runner.invoke(cli_mod._main_group, ["--help"])
    assert result.exit_code == 0
    for name in ("generate", "login", "logout", "setup", "status", "uninstall"):
        assert name in result.output


def test_login_prints_code_and_saved_path(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_login(*, show):
        show("Open https://auth.openai.com/codex/device and enter the code ABCD-1234 to sign in.")
        return tmp_path / "auth.json"

    monkeypatch.setattr(cli_mod._auth, "login_device", fake_login)
    result = runner.invoke(cli_mod._main_group, ["login"])
    assert result.exit_code == 0
    assert "ABCD-1234" in result.stderr
    assert result.stdout == f"Signed in. Session saved to {tmp_path / 'auth.json'}\n"


def test_login_failure_exits_1_with_message(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*, show):
        raise cli_mod._auth.OAuthResponsesError("Device-code sign-in is not enabled for this account.")

    monkeypatch.setattr(cli_mod._auth, "login_device", failing)
    result = runner.invoke(cli_mod._main_group, ["login"])
    assert result.exit_code == 1
    assert "Device-code sign-in is not enabled for this account." in result.stderr


def test_logout_removes_own_session(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    own = tmp_path / "own.json"
    own.write_text("{}")
    monkeypatch.setenv("CODEX_IMAGES_AUTH_FILE", str(own))
    first = runner.invoke(cli_mod._main_group, ["logout"])
    assert first.exit_code == 0 and first.stdout == f"Signed out. Removed {own}\n"
    assert not own.exists()
    second = runner.invoke(cli_mod._main_group, ["logout"])
    assert second.exit_code == 0 and second.stdout == "No codex-images session to remove.\n"


def test_version(runner: CliRunner) -> None:
    result = runner.invoke(cli_mod._main_group, ["--version"])
    assert result.exit_code == 0
    assert "codex-images-mcp" in result.output


def test_generate_without_prompt_exits_3(runner: CliRunner, fake_engine) -> None:
    result = runner.invoke(cli_mod._main_group, ["generate"])
    assert result.exit_code == 3
    assert "Provide a PROMPT or --prompt-json." in result.output
    assert fake_engine == []


def test_generate_prints_v2_json_with_local_paths(runner: CliRunner, fake_engine, tmp_path: Path) -> None:
    result = runner.invoke(cli_mod._main_group, ["generate", "a mug", "--transparent"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema_version"] == "codex-images.result/v2"
    assert payload["status"] == "completed"
    artifact = payload["artifacts"][0]
    assert Path(artifact["path"]).is_relative_to(tmp_path / "store")
    assert fake_engine[0]["prompt"] == "a mug" and fake_engine[0]["transparent"] is True


def test_generate_variants_and_exact_size(runner: CliRunner, fake_engine) -> None:
    result = runner.invoke(cli_mod._main_group, ["generate", "x", "-n", "2", "--width", "20", "--height", "20", "--size-policy", "exact"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["batch"]["completed"] == 2
    assert {(a["width"], a["height"]) for a in payload["artifacts"]} == {(20, 20)}


def test_generate_sends_references_and_mask_inline(runner: CliRunner, fake_engine, tmp_path: Path) -> None:
    ref, mask = _png(tmp_path / "ref.png"), _png(tmp_path / "mask.png")
    result = runner.invoke(cli_mod._main_group, ["generate", "edit", "-r", str(ref), "--mask", str(mask)])
    assert result.exit_code == 0, result.output
    sent = fake_engine[0]
    assert Path(sent["references"][0]).read_bytes() == ref.read_bytes()
    assert Path(sent["mask"]).read_bytes() == mask.read_bytes()
    assert Path(sent["references"][0]).is_relative_to(tmp_path / "store")


def test_generate_prompt_json(runner: CliRunner, fake_engine, tmp_path: Path) -> None:
    spec = tmp_path / "p.json"
    spec.write_text(json.dumps({"Primary request": "x"}), "utf-8")
    result = runner.invoke(cli_mod._main_group, ["generate", "--prompt-json", str(spec)])
    assert result.exit_code == 0, result.output
    assert fake_engine[0]["prompt"] == {"Primary request": "x"}


def test_generate_invalid_prompt_json_exits_3(runner: CliRunner, fake_engine, tmp_path: Path) -> None:
    spec = tmp_path / "p.json"
    spec.write_text("{not json", "utf-8")
    result = runner.invoke(cli_mod._main_group, ["generate", "--prompt-json", str(spec)])
    assert result.exit_code == 3
    assert fake_engine == []


def test_generate_failure_exits_2(runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODEX_IMAGES_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(cli_mod._native, "generate", lambda args: {"ok": False, "images": [], "error": "nope"})
    result = runner.invoke(cli_mod._main_group, ["generate", "x", "--pretty"])
    assert result.exit_code == 2
    assert "nope" in result.output



# ---------------------------------------------------------------------------
# 9) Installer subcommands — status / setup --dry-run / uninstall --dry-run
# ---------------------------------------------------------------------------


def test_status_command_runs_without_errors(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``imagen status`` prints a table and exits 0 without modifying anything."""
    import codex_images_mcp._install as _install_mod

    monkeypatch.setattr(_install_mod, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))

    result = runner.invoke(cli_mod._status_command, [])
    assert result.exit_code == 0
    # Table must contain all five client names.
    for name in ("Claude Code", "Claude Desktop", "Codex", "Cursor", "OpenCode"):
        assert name in result.output


def test_setup_dry_run_all_reports_plan(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``imagen setup --dry-run --all`` prints a dry-run plan and exits 0."""
    import codex_images_mcp._install as _install_mod
    import shutil

    monkeypatch.setattr(_install_mod, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    # Make Claude Code appear detected so there's at least one client.
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(cli_mod._setup_command, ["--dry-run", "--all"])
    assert result.exit_code == 0
    combined = result.output + (result.stderr or "")
    assert "dry-run" in combined.lower()


def test_setup_dry_run_specific_client(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``imagen setup --dry-run --client claude-code`` works without changes."""
    import codex_images_mcp._install as _install_mod

    monkeypatch.setattr(_install_mod, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    (tmp_path / ".claude.json").write_text("{}", encoding="utf-8")

    result = runner.invoke(
        cli_mod._setup_command, ["--dry-run", "--client", "claude-code"]
    )
    assert result.exit_code == 0
    # Config must not have been modified.
    import json as _json
    data = _json.loads((tmp_path / ".claude.json").read_text())
    assert "mcpServers" not in data


def test_uninstall_dry_run_all_no_changes(
    runner: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``imagen uninstall --dry-run --all`` exits 0 and changes nothing."""
    import codex_images_mcp._install as _install_mod
    import shutil

    monkeypatch.setattr(_install_mod, "_home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setattr(shutil, "which", lambda name: None)

    # Install first so there's something installed.
    import json as _json
    config = {"mcpServers": {"codex-images": {"command": "codex-images-mcp-server"}}}
    (tmp_path / ".claude.json").write_text(_json.dumps(config), encoding="utf-8")

    result = runner.invoke(cli_mod._uninstall_command, ["--dry-run", "--all"])
    assert result.exit_code == 0
    combined = result.output + (result.stderr or "")
    assert "dry-run" in combined.lower()
    # Config must be unchanged.
    data = _json.loads((tmp_path / ".claude.json").read_text())
    assert "codex-images" in data["mcpServers"]
