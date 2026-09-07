"""dango/cli/commands/mcp_setup.py

`dango mcp setup` / `dango mcp status` / `dango mcp remove` — LLM client
config detection, writing, and teardown. Split out of mcp_server.py to keep
that file (the FastMCP server definition + read tools) under the file-size
check; registers its commands onto `mcp_group` via decorator side effects on
import, mirroring the cross-file registration pattern in commands/remote.py +
remote_auth.py etc.

1.0.8-OPS-4 redesign (see BUGS-FOUND.md's two MCP entries, 2026-09-07, and
`v1.0.x-planning/1.0.8/OPS-4-mcp-config-redesign.md` for the full
investigation this rewrite is based on): the original version hand-wrote a
single **global** entry into each client's config file, with no per-project
scoping and no version-pinning safety check. Live investigation on
2026-09-08 found each client needs a genuinely different fix, not one design
applied three times:

- **Claude Code**: has a real private, per-project config scope (`local`,
  stored in ~/.claude.json keyed by absolute project path) reachable only
  through the client's own CLI (`claude mcp add/get/remove`), not by writing
  a file ourselves. Verified live: `claude mcp add --scope local <name>
  --env KEY=VAL -- <cmd> <args>` (env flags must come *after* the name --
  `-e/--env` is variadic and greedily swallows the next token if placed
  before it, a real bug found while testing this).
- **Cursor**: no native CLI install command exists, but its project-scoped
  `.cursor/mcp.json` supports `${workspaceFolder}` variable substitution
  (confirmed against Cursor's own docs) -- this makes a *portable*,
  safe-to-commit config possible: a bare `dango` command (PATH-resolved,
  not hardcoded to this machine's venv) plus `${workspaceFolder}` for
  DANGO_PROJECT_ROOT. This actually fixes both original bugs for Cursor
  (cross-project bleed AND version-pinning), better than Claude Code or
  Windsurf can manage.
- **Windsurf**: confirmed still global-only as of 2026-09-08 (no
  project-scoped config exists) -- a hard platform ceiling, not solvable
  here. Best-available mitigation: inject DANGO_PROJECT_ROOT into the global
  entry so at least the one project Windsurf is pointed at is unambiguous,
  and tell the user plainly that a second project will overwrite the first.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from dango.cli import console
from dango.cli.commands.mcp_server import mcp_group


def _resolve_dango_cmd() -> str:
    """Resolve the absolute path to the `dango` console script next to the
    active interpreter.

    Console scripts installed by pip always live next to the interpreter
    (<venv>/bin/dango on POSIX, <venv>\\Scripts\\dango.exe on Windows). A
    substring replace on sys.executable (e.g. "/bin/python" -> "/bin/dango")
    breaks for any interpreter binary named "pythonX.Y" (leaves a trailing
    version suffix, e.g. "/bin/dango3.11", which doesn't exist) and is a
    complete no-op on Windows (no "/bin/python" substring to replace), which
    would write sys.executable itself — python.exe — as the command. Falls
    back to the bare "dango" (relying on PATH) if no sibling script exists.
    """
    venv_dango = Path(sys.executable).parent / ("dango.exe" if sys.platform == "win32" else "dango")
    return str(venv_dango) if venv_dango.exists() else "dango"


def _setup_claude_code(project_root: Path, dango_cmd: str) -> list[str]:
    """Register dango via `claude mcp add --scope local` — the client's own
    install command — instead of hand-writing ~/.claude.json ourselves.
    Local scope is private to this OS user and keyed by absolute project
    path: it structurally eliminates both the cross-project-bleed bug (each
    project gets its own keyed entry) and, combined with DANGO_PROJECT_ROOT,
    narrows the wrong-project-resolution bug (the client resolves by project
    path, not by whatever cwd a session happened to launch from).

    Gated on ~/.claude existing (same detection heuristic as before) so we
    only attempt this — and only print the "claude CLI not on PATH" warning
    below — when there's actual signal Claude Code is installed. The `claude`
    CLI binary is a separate thing from the config directory: a prior
    install can leave ~/.claude in place while `claude` itself is missing or
    not on PATH in the *current* shell, which is a distinct, actionable
    problem from "Claude Code isn't installed at all".
    """
    if not (Path.home() / ".claude").exists():
        return []

    try:
        result = subprocess.run(
            [
                "claude",
                "mcp",
                "add",
                "--scope",
                "local",
                "dango",
                "--env",
                f"DANGO_PROJECT_ROOT={project_root}",
                "--",
                dango_cmd,
                "mcp",
                "run",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        console.print(
            "[yellow]⚠[/yellow]  Claude Code detected (~/.claude exists) but the `claude` "
            "CLI isn't on PATH — skipping. Ensure `claude` is on your PATH and re-run "
            "`dango mcp setup`."
        )
        return []

    if result.returncode != 0:
        console.print(
            f"[yellow]⚠[/yellow]  Claude Code: `claude mcp add` failed: {result.stderr.strip()}"
        )
        return []
    return ["Claude Code"]


def _setup_cursor(project_root: Path) -> list[str]:
    """Write a project-scoped, git-committable `.cursor/mcp.json`.

    Cursor has no native CLI install command (checked `cursor --help` — not
    on PATH on the investigation machine — and Cursor's own docs, which
    don't mention one). Its project-scoped `.cursor/mcp.json` is explicitly
    meant to be committed and shared with the whole team (Cursor's own
    docs), which would normally make an absolute, machine-specific venv path
    unacceptable — but Cursor supports `${workspaceFolder}` variable
    substitution (confirmed against Cursor's own docs), so a bare `dango`
    command (resolved via PATH once each teammate activates their own venv)
    plus `${workspaceFolder}` for DANGO_PROJECT_ROOT is genuinely portable
    and safe to commit. This fixes both original bugs for Cursor — no
    absolute path, no global/shared entry.

    Still gated on ~/.cursor existing: the app creates this on install, and
    remains a reasonable "is Cursor installed at all" signal even though the
    config itself now goes into the project instead of ~/.cursor/mcp.json.
    """
    if not (Path.home() / ".cursor").exists():
        return []

    entry = {
        "command": "dango",
        "args": ["mcp", "run"],
        "env": {"DANGO_PROJECT_ROOT": "${workspaceFolder}"},
    }
    _write_mcp_config(project_root / ".cursor" / "mcp.json", entry)
    _print_git_warnings(project_root)
    return ["Cursor"]


def _setup_windsurf(project_root: Path, dango_cmd: str) -> list[str]:
    """Write the global Windsurf config, with DANGO_PROJECT_ROOT injected.

    Windsurf has no project-scoped MCP config at all — confirmed against
    Windsurf's own 2026 changelog/docs, still true as of 2026-09-08. This is
    a hard platform ceiling, not something dango can work around. Injecting
    DANGO_PROJECT_ROOT at least makes the *one* project Windsurf is
    configured for unambiguous; it does not eliminate cross-project bleed
    for Windsurf specifically (documented here and in
    docs/guides/mcp-claude-code.md, not silently shipped as if it were
    fixed).
    """
    windsurf_config = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    if not windsurf_config.parent.exists():
        return []

    entry = {
        "command": dango_cmd,
        "args": ["mcp", "run"],
        "env": {"DANGO_PROJECT_ROOT": str(project_root)},
    }
    _write_mcp_config(windsurf_config, entry)
    console.print(
        "[yellow]Note:[/yellow] Windsurf only supports one Dango project at a time — "
        "connecting a second project will overwrite this configuration."
    )
    return ["Windsurf"]


def _print_git_warnings(project_root: Path) -> None:
    """Non-blocking git-state warnings for writing `.cursor/mcp.json` into
    the project's own repo tree. Mirrors source_wizard.py's and
    model_wizard.py's `_print_git_warnings()` (1.0.8-OPS-3) — this is the
    first time `mcp setup` mutates a file inside the project repo itself
    rather than a client's own dotfiles, so it gets the same treatment as
    other repo-mutating commands."""
    from dango.cli.commands.mcp_helpers import _git_warnings

    for w in _git_warnings(project_root):
        console.print(f"  [yellow]Warning:[/yellow] {w}")


@mcp_group.command("setup")
@click.pass_context
def mcp_setup(ctx: click.Context) -> None:
    """Detect installed LLM clients and configure them to use dango mcp."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)
    dango_cmd = _resolve_dango_cmd()

    configured: list[str] = []
    configured += _setup_claude_code(project_root, dango_cmd)
    configured += _setup_cursor(project_root)
    configured += _setup_windsurf(project_root, dango_cmd)

    if not configured:
        console.print("\n[yellow]No LLM clients detected.[/yellow]")
        console.print("Supported: Claude Code, Cursor, Windsurf")
        console.print("Install one and run `dango mcp setup` again.\n")
        return

    console.print()
    for client in configured:
        console.print(f"[green]✓[/green] {client} — MCP configured")
    console.print()
    console.print("[dim]Restart your LLM client to activate.[/dim]")
    console.print("[dim]Run `dango mcp status` to verify the connection.[/dim]\n")


def _print_status_from_file(client: str, path: Path) -> None:
    """Read a client's own JSON config file directly and report whether the
    `dango` key is present. Used for Cursor and Windsurf, which still use
    the hand-written-file mechanism (Claude Code's status check goes through
    `claude mcp get` instead — see mcp_status() — because its local-scope
    entry lives inside ~/.claude.json's internal format, which is Claude
    Code's implementation detail, not a stable contract for us to parse)."""
    if not path.exists():
        console.print(f"[yellow]⚠[/yellow]  {client}: not configured — run `dango mcp setup`")
        return
    try:
        cfg = json.loads(path.read_text())
        servers = cfg.get("mcpServers", {})
        if "dango" in servers:
            console.print(f"[green]✓[/green] {client}: dango MCP configured")
        else:
            console.print(
                f"[yellow]⚠[/yellow]  {client}: dango not in mcpServers — run `dango mcp setup`"
            )
    except Exception:
        console.print(f"[red]✗[/red] {client}: config file unreadable")


@mcp_group.command("status")
@click.pass_context
def mcp_status(ctx: click.Context) -> None:
    """Verify MCP configuration is correct for detected LLM clients."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)

    console.print()
    found_any = False

    if (Path.home() / ".claude").exists():
        found_any = True
        try:
            # `claude mcp get` takes no --scope flag (verified live,
            # 2026-09-08) — it searches all scopes and reports which one it
            # found the entry in. Run with cwd=project_root so this resolves
            # correctly regardless of which subdirectory `dango mcp status`
            # itself was invoked from (Claude Code's own local-scope lookup
            # walks up from cwd the same way find_project_root() does,
            # confirmed live).
            result = subprocess.run(
                ["claude", "mcp", "get", "dango"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
            )
            if result.returncode == 0 and "Local config" in result.stdout:
                console.print("[green]✓[/green] Claude Code: dango MCP configured (local scope)")
            else:
                console.print(
                    "[yellow]⚠[/yellow]  Claude Code: dango not configured for this project — "
                    "run `dango mcp setup`"
                )
        except FileNotFoundError:
            console.print(
                "[yellow]⚠[/yellow]  Claude Code: `claude` CLI not found on PATH — can't verify"
            )

    if (Path.home() / ".cursor").exists():
        found_any = True
        _print_status_from_file("Cursor", project_root / ".cursor" / "mcp.json")

    windsurf_config = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    if windsurf_config.parent.exists():
        found_any = True
        _print_status_from_file("Windsurf", windsurf_config)

    if not found_any:
        console.print("[dim]No LLM clients detected.[/dim]")
    console.print()


@mcp_group.command("remove")
@click.pass_context
def mcp_remove(ctx: click.Context) -> None:
    """Remove dango MCP configuration from all detected LLM clients."""
    from dango.cli.utils import require_project_context

    project_root = require_project_context(ctx)

    removed: list[str] = []

    if (Path.home() / ".claude").exists():
        try:
            result = subprocess.run(
                ["claude", "mcp", "remove", "dango", "--scope", "local"],
                capture_output=True,
                text=True,
                cwd=str(project_root),
            )
            if result.returncode == 0:
                removed.append("Claude Code")
            else:
                message = result.stderr.strip() or result.stdout.strip()
                console.print(
                    f"[yellow]⚠[/yellow]  Claude Code: `claude mcp remove` reported: {message}"
                )
        except FileNotFoundError:
            console.print(
                "[yellow]⚠[/yellow]  Claude Code: `claude` CLI not found on PATH — skipping"
            )

    if _remove_mcp_config(project_root / ".cursor" / "mcp.json"):
        removed.append("Cursor")

    windsurf_config = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    if _remove_mcp_config(windsurf_config):
        removed.append("Windsurf")

    if not removed:
        console.print("\n[yellow]Nothing to remove — no dango MCP configuration found.[/yellow]\n")
        return

    console.print()
    for client in removed:
        console.print(f"[green]✓[/green] {client} — MCP configuration removed")
    console.print()


def _atomic_write_json(config_path: Path, data: dict, original_mode: int | None) -> None:
    """Write *data* to config_path atomically (temp file + os.replace),
    preserving original_mode (or defaulting to 0644 for a new file).

    Shared by `_write_mcp_config` (add/update the `dango` key) and
    `_remove_mcp_config` (delete it) so the crash-safety behavior — this is
    the user's real LLM client config file, which may hold unrelated
    settings; an interrupted direct write (crash, kill, laptop sleep
    mid-write) would leave it truncated or corrupted, not just the MCP
    section — lives in exactly one place instead of being duplicated
    between add and remove.
    """
    import os
    import tempfile

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent, prefix=f".{config_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2))
        # tempfile.mkstemp() always creates its file at mode 0600 regardless
        # of the target's prior permissions. Without this, every write would
        # silently tighten the config file from its actual mode (typically
        # 0644) down to 0600. Preserve the original mode, or use a normal
        # 0644 default the first time the file is created.
        os.chmod(tmp_path, original_mode if original_mode is not None else 0o644)
        os.replace(tmp_path, config_path)
    except BaseException:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def _write_mcp_config(config_path: Path, entry: dict) -> None:
    """Write the dango MCP entry into an LLM client config file.

    Claude Code, Cursor, and Windsurf all use {"mcpServers": {...}} at the
    config file's root. (Claude Code no longer goes through this path as of
    1.0.8-OPS-4 — see `_setup_claude_code()` — but Cursor and Windsurf
    still do.)
    """
    import stat

    existing: dict = {}
    original_mode: int | None = None
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except Exception:
            pass
        original_mode = stat.S_IMODE(config_path.stat().st_mode)

    existing.setdefault("mcpServers", {})["dango"] = entry
    _atomic_write_json(config_path, existing, original_mode)


def _remove_mcp_config(config_path: Path) -> bool:
    """Delete the `dango` key from an LLM client config file, atomically.

    Returns True if a `dango` entry was actually present and removed, False
    if there was nothing to remove (file missing, unreadable, or no `dango`
    key) — callers use this to decide whether to report the client as
    "MCP configuration removed".
    """
    import stat

    if not config_path.exists():
        return False
    try:
        existing = json.loads(config_path.read_text())
    except Exception:
        return False

    servers = existing.get("mcpServers", {})
    if "dango" not in servers:
        return False
    del servers["dango"]

    original_mode = stat.S_IMODE(config_path.stat().st_mode)
    _atomic_write_json(config_path, existing, original_mode)
    return True
