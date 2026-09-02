"""dango/cli/commands/mcp_setup.py

`dango mcp setup` / `dango mcp status` — LLM client config detection and
writing. Split out of mcp_server.py to keep that file (the FastMCP server
definition + read tools) under the file-size check; registers its commands
onto `mcp_group` via decorator side effects on import, mirroring the
cross-file registration pattern in commands/remote.py + remote_auth.py etc.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from dango.cli import console
from dango.cli.commands.mcp_server import mcp_group


@mcp_group.command("setup")
@click.pass_context
def mcp_setup(ctx: click.Context) -> None:
    """Detect installed LLM clients and configure them to use dango mcp."""
    import sys

    dango_cmd = sys.executable.replace("/bin/python", "/bin/dango")
    # Fall back to `dango` on PATH if we can't determine the venv path
    if not Path(dango_cmd).exists():
        dango_cmd = "dango"

    config_entry = {"command": dango_cmd, "args": ["mcp", "run"]}
    configured = []

    # Claude Code: ~/.claude/settings.json
    claude_settings = Path.home() / ".claude" / "settings.json"
    if claude_settings.parent.exists():
        _write_mcp_config(claude_settings, config_entry)
        configured.append("Claude Code")

    # Cursor: ~/.cursor/mcp.json
    cursor_config = Path.home() / ".cursor" / "mcp.json"
    if cursor_config.parent.exists():
        _write_mcp_config(cursor_config, config_entry)
        configured.append("Cursor")

    # Windsurf: ~/.codeium/windsurf/mcp_config.json
    windsurf_config = Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    if windsurf_config.parent.exists():
        _write_mcp_config(windsurf_config, config_entry)
        configured.append("Windsurf")

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


@mcp_group.command("status")
@click.pass_context
def mcp_status(ctx: click.Context) -> None:
    """Verify MCP configuration is correct for detected LLM clients."""
    checks = {
        "Claude Code": Path.home() / ".claude" / "settings.json",
        "Cursor": Path.home() / ".cursor" / "mcp.json",
        "Windsurf": Path.home() / ".codeium" / "windsurf" / "mcp_config.json",
    }

    console.print()
    found_any = False
    for client, path in checks.items():
        if not path.parent.exists():
            continue
        found_any = True
        if not path.exists():
            console.print(f"[yellow]⚠[/yellow]  {client}: not configured — run `dango mcp setup`")
            continue
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

    if not found_any:
        console.print("[dim]No LLM clients detected.[/dim]")
    console.print()


def _write_mcp_config(config_path: Path, entry: dict) -> None:
    """Write the dango MCP entry into an LLM client config file.

    Claude Code, Cursor, and Windsurf all use {"mcpServers": {...}} at the
    config file's root.
    """
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except Exception:
            pass

    existing.setdefault("mcpServers", {})["dango"] = entry

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2))
