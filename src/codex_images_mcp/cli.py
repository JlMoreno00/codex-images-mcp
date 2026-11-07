"""codex_images_mcp.cli — the ``codex-images-mcp`` command.

Subcommands
-----------
``generate``   generate or edit images through the native Codex Images engine
``login``      sign in to ChatGPT with a device code (a session separate from Codex)
``logout``     delete the codex-images session (never touches a Codex CLI login)
``setup``      register the codex-images MCP server in your AI clients
``uninstall``  remove the codex-images registration (never touches codex-imagen)
``status``     show detected clients and install state

``generate`` prints the ``codex-images.result/v2`` JSON on stdout (``--pretty``
for a human summary) and writes nothing else to stdout. Exit codes: 0 when
every image completed, 2 when generation failed or was partial, 3 for invalid
arguments.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import click

from codex_images_mcp import __version__, _auth, _native, _service

EXIT_OK: int = 0
EXIT_GENERATION_FAILURE: int = 2
EXIT_INVALID_ARGS: int = 3


class _InvalidArgsError(click.ClickException):
    """Our own argument validation errors exit with code 3."""

    exit_code = EXIT_INVALID_ARGS


def _inline_reference(path: Path, role: str = "content") -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _InvalidArgsError(f"Cannot read reference {path}: {exc.strerror}") from exc
    return {"source": {"type": "inline_blob", "data": base64.b64encode(data).decode("ascii")}, "role": role}


def _load_prompt(prompt: str | None, prompt_json: Path | None) -> Any:
    if prompt_json is not None:
        try:
            value = json.loads(prompt_json.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise _InvalidArgsError(f"--prompt-json is not readable JSON: {exc}") from exc
        if not isinstance(value, (str, dict, list)):
            raise _InvalidArgsError("--prompt-json must contain a string, object or list.")
        return value
    if not prompt:
        raise _InvalidArgsError("Provide a PROMPT or --prompt-json.")
    return prompt


@click.command(name="generate")
@click.argument("prompt", required=False)
@click.option("--prompt-json", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="JSON file with a prompt string, labeled-spec object or list of prompts.")
@click.option("-r", "--reference", "references", multiple=True,
              type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Reference image to edit (repeatable).")
@click.option("--mask", type=click.Path(exists=True, dir_okay=False, path_type=Path), help="Edit mask; requires --reference.")
@click.option("-n", "--count", type=click.IntRange(min=1), default=None, help="Number of variants of one prompt.")
@click.option("--transparent", is_flag=True, help="Request a native transparent background.")
@click.option("--format", "output_format", type=click.Choice(["png", "jpeg", "webp"]), default="png", show_default=True)
@click.option("--width", type=click.IntRange(1, 8192), default=None)
@click.option("--height", type=click.IntRange(1, 8192), default=None)
@click.option("--size-policy", type=click.Choice(["native", "exact", "fit", "cover"]), default="native", show_default=True)
@click.option("-o", "--output-dir", type=click.Path(file_okay=False, path_type=Path), default=None,
              help="Artifact store (default: CODEX_IMAGES_OUTPUT_DIR or your user data directory).")
@click.option("--model", default=None, help="GPT Image model id (API engine only), e.g. gpt-image-2.6-sunburst.")
@click.option("--quality", type=click.Choice(["auto", "low", "medium", "high", "xhigh", "max"]), default=None,
              help="Rendering quality (API engine only).")
@click.option("--complexity", type=click.Choice(["low", "medium", "high"]), default=None,
              help="Planning effort before rendering (API engine only).")
@click.option("--pretty", is_flag=True, help="Print a human summary instead of JSON.")
def _generate_command(prompt, prompt_json, references, mask, count, transparent, output_format,
                      width, height, size_policy, output_dir, model, quality, complexity, pretty) -> None:
    """Generate or edit images with your ChatGPT plan."""
    arguments: dict[str, Any] = {"prompt": _load_prompt(prompt, prompt_json)}
    if references:
        arguments["references"] = [_inline_reference(path) for path in references]
    if mask is not None:
        arguments["mask"] = _inline_reference(mask)
    if count is not None:
        arguments["batch_mode"], arguments["count"] = "variants", count
    if transparent:
        arguments["transparent"] = True
    for field, value in (("model", model), ("quality", quality), ("complexity", complexity)):
        if value is not None:
            arguments[field] = value
    output: dict[str, Any] = {"format": output_format, "size_policy": size_policy}
    if width is not None or height is not None:
        output.update(width=width, height=height)
    arguments["output"] = output

    result = _service.generate(arguments, _native.generate, output_dir=output_dir)
    structured = {**result["structured"], "artifacts": [
        {**item, "path": next((a["path"] for a in result["artifacts"] if a["index"] == item["index"]), None)}
        if item.get("status") == "completed" else item
        for item in result["structured"]["artifacts"]
    ]}
    if pretty:
        click.echo(result["summary"])
        for item in structured["artifacts"]:
            if item.get("path"):
                click.echo(f"  {item['path']}  {item['width']}x{item['height']}  {item['bytes']} bytes")
        for warning in structured["warnings"]:
            click.echo(f"warning: {warning}", err=True)
        for error in structured["errors"]:
            click.echo(f"error: {error['code']}: {error['message']}", err=True)
    else:
        click.echo(json.dumps(structured, ensure_ascii=False))
    raise SystemExit(EXIT_GENERATION_FAILURE if result["is_error"] else EXIT_OK)


@click.command(name="login")
def _login_command() -> None:
    """Sign in to ChatGPT with a device code (works on headless servers)."""
    try:
        path = _auth.login_device(show=lambda message: click.echo(message, err=True))
    except _auth.OAuthResponsesError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Signed in. Session saved to {path}")


@click.command(name="logout")
def _logout_command() -> None:
    """Delete the codex-images ChatGPT session."""
    if _auth.logout():
        click.echo(f"Signed out. Removed {_auth.own_auth_path()}")
    else:
        click.echo("No codex-images session to remove.")


# ---------------------------------------------------------------------------
# Installer subcommands — setup / uninstall / status
# ---------------------------------------------------------------------------


def _is_tty() -> bool:
    """Return True when stdout is an interactive terminal."""
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


def _styled(text: str, **kwargs: Any) -> str:
    """Apply click.style only when stdout is a TTY."""
    if _is_tty():
        return click.style(text, **kwargs)
    return text


def _print_client_table(statuses: list) -> None:  # type: ignore[type-arg]
    """Print a formatted table of client detection and install status."""
    from codex_images_mcp._install import ClientStatus

    header = (
        f"{'#':<3}  {'Client':<16}  {'Detected':<10}  {'Installed':<10}  Config"
    )
    click.echo(header)
    click.echo("-" * 75)
    for i, s in enumerate(statuses, start=1):
        detected_str = _styled("yes", fg="green") if s.detected else _styled("no", fg="red")
        if s.detected:
            installed_str = _styled("yes", fg="green") if s.installed else _styled("no", fg="yellow")
        else:
            installed_str = "-"
        config_str = str(s.config_path) if s.config_path else "(not found)"
        click.echo(f"{i:<3}  {s.name:<16}  {detected_str:<10}  {installed_str:<10}  {config_str}")


# ---------------------------------------------------------------------------
# codex-images-mcp status
# ---------------------------------------------------------------------------

@click.command(name="status")
def _status_command() -> None:
    """Show detected MCP clients and whether codex-images is registered."""
    from codex_images_mcp._install import detect_clients, skill_status_for_client

    statuses = detect_clients()
    click.echo("codex-images MCP installer — client status\n")
    _print_client_table(statuses)
    click.echo()
    installed_count = sum(1 for s in statuses if s.installed)
    detected_count = sum(1 for s in statuses if s.detected)
    click.echo(
        f"Detected: {detected_count}/5  |  Installed: {installed_count}/5"
    )

    # Skill status table.
    click.echo()
    click.echo("Bundled skill (codex-images) status:")
    click.echo(f"  {'Client':<16}  {'Skill':<10}  Path")
    click.echo("  " + "-" * 60)
    for s in statuses:
        skill_installed, skill_path = skill_status_for_client(s.key)
        if skill_installed is None:
            skill_str = "-"
            path_str = "(not supported)"
        elif skill_installed:
            skill_str = _styled("installed", fg="green")
            path_str = str(skill_path)
        else:
            skill_str = _styled("missing", fg="yellow")
            path_str = str(skill_path) if skill_path else "(unknown)"
        click.echo(f"  {s.name:<16}  {skill_str:<10}  {path_str}")


# ---------------------------------------------------------------------------
# codex-images-mcp setup
# ---------------------------------------------------------------------------

@click.command(name="setup")
@click.option(
    "--all",
    "install_all",
    is_flag=True,
    default=False,
    help="Install for all detected clients without prompting.",
)
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice(
        ["claude-code", "claude-desktop", "codex", "cursor", "opencode"]
    ),
    help="Install for specific client(s). Repeatable.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making any changes.",
)
def _setup_command(install_all: bool, clients: tuple[str, ...], dry_run: bool) -> None:
    """Register codex-images MCP in one or more AI clients.

    Run without flags for an interactive walkthrough. Use --all to install
    for every detected client, or --client NAME to target specific clients.
    """
    from codex_images_mcp._install import (
        ClientStatus,
        detect_clients,
        install_for_client,
        write_codex_preference_snippet,
    )

    all_statuses = detect_clients()

    if dry_run:
        click.echo("[dry-run] No files will be modified.\n")

    # Determine which client keys to install.
    if clients:
        # Explicit --client flags.
        target_keys = list(clients)
    elif install_all:
        # All detected clients.
        target_keys = [s.key for s in all_statuses if s.detected]
        if not target_keys:
            click.echo("No supported clients detected on this system.")
            sys.exit(0)
    else:
        # Interactive walkthrough.
        click.echo("codex-images MCP installer\n")
        _print_client_table(all_statuses)
        click.echo()

        detected_keys = [s.key for s in all_statuses if s.detected]
        if not detected_keys:
            click.echo("No supported clients detected. Nothing to install.")
            sys.exit(0)

        click.echo(
            "Install for which clients? (comma-separated numbers, 'a' for all detected, 'q' to quit):"
        )
        for i, s in enumerate(all_statuses, start=1):
            if s.detected:
                click.echo(f"  {i}  {s.name}")

        raw = click.prompt("Selection", default="a")
        if raw.strip().lower() == "q":
            click.echo("Aborted.")
            sys.exit(0)

        if raw.strip().lower() == "a":
            target_keys = detected_keys
        else:
            # Parse comma-separated numbers.
            target_keys = []
            for part in raw.split(","):
                part = part.strip()
                if not part.isdigit():
                    click.echo(f"  Skipping invalid selection: {part!r}", err=True)
                    continue
                idx = int(part) - 1
                if 0 <= idx < len(all_statuses):
                    s = all_statuses[idx]
                    if s.detected:
                        target_keys.append(s.key)
                    else:
                        click.echo(
                            f"  Skipping {s.name} — not detected on this system.",
                            err=True,
                        )
                else:
                    click.echo(f"  Number {part} out of range.", err=True)

        if not target_keys:
            click.echo("No clients selected. Aborted.")
            sys.exit(0)

        # Confirmation.
        name_map = {s.key: s.name for s in all_statuses}
        names = ", ".join(name_map.get(k, k) for k in target_keys)
        click.echo(f"\nWill install codex-images MCP for: {names}")
        if not click.confirm("Proceed?", default=True):
            click.echo("Aborted.")
            sys.exit(0)

    # Execute installs.
    click.echo()
    success_count = 0
    codex_selected = "codex" in target_keys
    from codex_images_mcp._install import install_skill_for_client
    for key in target_keys:
        ok, msg = install_for_client(key, dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")
        if ok:
            success_count += 1
        # Also install the bundled skill alongside the MCP config.
        s_ok, s_msg = install_skill_for_client(key, dry_run=dry_run)
        s_prefix = _styled("[OK]", fg="green") if s_ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {s_prefix}  skill: {s_msg}")

    # Codex AGENTS.md preference snippet.
    if codex_selected and not install_all and not clients:
        # Interactive: ask the user.
        click.echo()
        if click.confirm(
            "Add codex-images preference instruction to ~/.codex/AGENTS.md "
            "(so the AI prefers codex-images over the built-in imagegen)?",
            default=True,
        ):
            ok, msg = write_codex_preference_snippet(dry_run=dry_run)
            prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
            click.echo(f"  {prefix}  {msg}")
    elif codex_selected and (install_all or clients):
        # Non-interactive: always write the snippet.
        ok, msg = write_codex_preference_snippet(dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")

    click.echo()
    if dry_run:
        click.echo(f"[dry-run complete] Would have installed for {len(target_keys)} client(s).")
    else:
        click.echo(
            f"Done. Installed for {success_count}/{len(target_keys)} client(s)."
        )
        if success_count < len(target_keys):
            sys.exit(1)


# ---------------------------------------------------------------------------
# codex-images-mcp uninstall
# ---------------------------------------------------------------------------

@click.command(name="uninstall")
@click.option(
    "--all",
    "uninstall_all",
    is_flag=True,
    default=False,
    help="Uninstall from all clients without prompting.",
)
@click.option(
    "--client",
    "clients",
    multiple=True,
    type=click.Choice(
        ["claude-code", "claude-desktop", "codex", "cursor", "opencode"]
    ),
    help="Uninstall from specific client(s). Repeatable.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show what would happen without making any changes.",
)
def _uninstall_command(
    uninstall_all: bool, clients: tuple[str, ...], dry_run: bool
) -> None:
    """Remove the codex-images MCP registration from AI clients."""
    from codex_images_mcp._install import (
        detect_clients,
        remove_codex_preference_snippet,
        uninstall_for_client,
    )

    all_statuses = detect_clients()

    if dry_run:
        click.echo("[dry-run] No files will be modified.\n")

    if clients:
        target_keys = list(clients)
    elif uninstall_all:
        target_keys = [s.key for s in all_statuses if s.installed]
        if not target_keys:
            click.echo("codex-images is not installed in any detected client.")
            sys.exit(0)
    else:
        # Interactive.
        click.echo("codex-images MCP uninstaller\n")
        _print_client_table(all_statuses)
        click.echo()

        installed_keys = [s.key for s in all_statuses if s.installed]
        if not installed_keys:
            click.echo("codex-images is not installed in any detected client.")
            sys.exit(0)

        click.echo(
            "Uninstall from which clients? (comma-separated numbers, 'a' for all installed, 'q' to quit):"
        )
        for i, s in enumerate(all_statuses, start=1):
            if s.installed:
                click.echo(f"  {i}  {s.name}")

        raw = click.prompt("Selection", default="a")
        if raw.strip().lower() == "q":
            click.echo("Aborted.")
            sys.exit(0)

        if raw.strip().lower() == "a":
            target_keys = installed_keys
        else:
            target_keys = []
            for part in raw.split(","):
                part = part.strip()
                if not part.isdigit():
                    continue
                idx = int(part) - 1
                if 0 <= idx < len(all_statuses):
                    target_keys.append(all_statuses[idx].key)

        if not target_keys:
            click.echo("No clients selected. Aborted.")
            sys.exit(0)

        name_map = {s.key: s.name for s in all_statuses}
        names = ", ".join(name_map.get(k, k) for k in target_keys)
        click.echo(f"\nWill remove codex-images MCP from: {names}")
        if not click.confirm("Proceed?", default=True):
            click.echo("Aborted.")
            sys.exit(0)

    click.echo()
    success_count = 0
    codex_selected = "codex" in target_keys
    from codex_images_mcp._install import uninstall_skill_for_client
    for key in target_keys:
        ok, msg = uninstall_for_client(key, dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")
        if ok:
            success_count += 1
        # Also remove the bundled skill.
        s_ok, s_msg = uninstall_skill_for_client(key, dry_run=dry_run)
        s_prefix = _styled("[OK]", fg="green") if s_ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {s_prefix}  skill: {s_msg}")

    if codex_selected:
        ok, msg = remove_codex_preference_snippet(dry_run=dry_run)
        prefix = _styled("[OK]", fg="green") if ok else _styled("[FAIL]", fg="red")
        click.echo(f"  {prefix}  {msg}")

    click.echo()
    if dry_run:
        click.echo(f"[dry-run complete] Would have uninstalled from {len(target_keys)} client(s).")
    else:
        click.echo(
            f"Done. Removed from {success_count}/{len(target_keys)} client(s)."
        )
        if success_count < len(target_keys):
            sys.exit(1)


# ---------------------------------------------------------------------------


@click.group(name="codex-images-mcp", context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.version_option(__version__, "--version", prog_name="codex-images-mcp")
def _main_group() -> None:
    """codex-images-mcp — Codex Images (gpt-image-2) on your ChatGPT plan.

    \b
    Examples:
      codex-images-mcp login              # sign in to ChatGPT once
      codex-images-mcp generate "a ceramic mug" --transparent
      codex-images-mcp setup              # register the MCP server in your clients
      codex-images-mcp status
      codex-images-mcp uninstall --all
    """


_main_group.add_command(_generate_command)
_main_group.add_command(_login_command)
_main_group.add_command(_logout_command)
_main_group.add_command(_setup_command)
_main_group.add_command(_uninstall_command)
_main_group.add_command(_status_command)


def main_group() -> None:
    """Console-script entry point."""
    _main_group.main()


if __name__ == "__main__":  # pragma: no cover
    main_group()
