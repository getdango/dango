"""dango/cli/commands/upgrade.py

Local Dango upgrade command and PyPI version cache helper.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import click

from dango.cli import console

_VERSION_CACHE_TTL = 86400  # 24 hours in seconds
_NEGATIVE_CACHE_TTL = 300  # 5 minutes — avoid hammering a down PyPI


def get_latest_version_cached(project_root: Path) -> str | None:
    """Check PyPI for the latest getdango version, with 24h file cache.

    Returns the latest version string, or ``None`` if the check fails.
    Cache location: ``.dango/state/version_check.json``.

    Negative results (PyPI unreachable) are cached for 5 minutes to avoid
    repeated slow network calls on every ``dango status`` invocation.
    """
    import json
    from datetime import datetime, timezone

    cache_path = project_root / ".dango" / "state" / "version_check.json"

    # Try cache first
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            checked_at = datetime.fromisoformat(data["checked_at"])
            age = (datetime.now(timezone.utc) - checked_at).total_seconds()
            cached_version: str | None = data.get("version")
            ttl = _VERSION_CACHE_TTL if cached_version else _NEGATIVE_CACHE_TTL
            if age < ttl:
                return cached_version
        except Exception:  # noqa: BLE001
            pass  # Corrupt cache — fall through to PyPI

    # Fetch from PyPI
    from dango.platform.cloud.server_status import check_latest_pypi_version

    latest = check_latest_pypi_version()
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "version": latest,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
    except OSError:
        pass  # Non-critical — can't write cache
    return latest


def _validate_version(version_str: str) -> None:
    """Validate version string format (X.Y.Z).

    Raises:
        click.BadParameter: If the version string is invalid.
    """
    if not re.match(r"^\d+\.\d+\.\d+$", version_str):
        raise click.BadParameter(
            f"Invalid version: {version_str!r}. Expected format: X.Y.Z (e.g. 1.2.3)"
        )


@click.command()
@click.option(
    "--version",
    "target_version",
    default=None,
    help="Specific version to install (e.g. 1.2.3). Defaults to latest on PyPI.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts.")
@click.pass_context
def upgrade(ctx: click.Context, target_version: str | None, yes: bool) -> None:
    """Upgrade Dango to the latest version (or a specific version).

    Upgrades the ``getdango`` package via pip, then runs any pending
    database migrations.  Restart services with ``dango start`` after
    upgrading.
    """
    from ..utils import require_project_context

    project_root = require_project_context(ctx)

    # Validate --version early (before network call)
    if target_version is not None:
        _validate_version(target_version)

    # Get current version
    from dango import __version__ as current_version

    # Determine target version
    if target_version is None:
        console.print("[bold]Checking for updates...[/bold]")
        latest = get_latest_version_cached(project_root)
        if latest is None:
            console.print(
                "[red]Error:[/red] Could not determine latest version from PyPI.\n"
                "Specify a version with [bold]--version X.Y.Z[/bold]."
            )
            raise SystemExit(1)
        target_version = latest

    # Compare versions
    from packaging.version import Version

    if Version(current_version) == Version(target_version):
        console.print(f"[green]Already at version {current_version} — no upgrade needed.[/green]")
        return

    if Version(current_version) > Version(target_version):
        direction = "downgrade"
    else:
        direction = "upgrade"

    # Display version change
    console.print(f"  Current version: [cyan]{current_version}[/cyan]")
    console.print(f"  Target version:  [cyan]{target_version}[/cyan]")
    if direction == "downgrade":
        console.print("  [yellow]Note: This is a downgrade.[/yellow]")
    console.print()

    # Pre-flight: verify pip is available
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        console.print(
            "[red]Error:[/red] pip is not available. "
            "Ensure pip is installed in the current Python environment."
        )
        raise SystemExit(1) from exc

    # Pre-flight: check disk space (warn if < 200 MB free)
    try:
        usage = shutil.disk_usage(sys.prefix)
        free_mb = usage.free // (1024 * 1024)
        if free_mb < 200:
            console.print(
                f"[yellow]Warning:[/yellow] Low disk space ({free_mb} MB free). Upgrade may fail."
            )
    except OSError:
        pass  # Non-critical

    # Confirmation prompts (unless --yes)
    if not yes:
        console.print("[dim]Tip: Run 'dango stop' first if services are running.[/dim]")
        if click.confirm("Pause to create a manual backup first?", default=True):
            console.print(
                "\n  Copy your [cyan].dango/[/cyan] directory or run your "
                "backup procedure in another terminal, then continue.\n"
            )
            if not click.confirm("Ready to proceed with upgrade?"):
                console.print("[yellow]Upgrade cancelled.[/yellow]")
                return
        elif not click.confirm(f"Proceed with {direction}?"):
            console.print("[yellow]Upgrade cancelled.[/yellow]")
            return

    # Run pip install
    console.print()
    console.print(f"[bold]Installing getdango=={target_version}...[/bold]")

    try:
        pip_result = subprocess.run(
            [sys.executable, "-m", "pip", "install", f"getdango=={target_version}", "--quiet"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired as exc:
        console.print("[red]Error:[/red] pip install timed out after 5 minutes.")
        raise SystemExit(1) from exc

    if pip_result.returncode != 0:
        console.print("[red]Error:[/red] pip install failed.")
        stderr = pip_result.stderr.strip()
        if stderr:
            for line in stderr.splitlines()[-5:]:
                console.print(f"  [dim]{line}[/dim]")
        raise SystemExit(1)

    console.print(f"  [green]Installed getdango=={target_version}[/green]")

    # Run pending migrations
    console.print("[bold]Running database migrations...[/bold]")
    try:
        from dango.migrations import apply_all_pending

        migration_results = apply_all_pending(project_root)
        total_applied = sum(len(v) for v in migration_results.values())
        if total_applied == 0:
            console.print("  [green]No pending migrations.[/green]")
        else:
            for db_name, applied in migration_results.items():
                if applied:
                    console.print(
                        f"  [green]Applied {len(applied)} migration(s) to {db_name}[/green]"
                    )
    except Exception as exc:
        console.print(f"[red]Migration error:[/red] {exc}")
        # NOTE: ``dango restore`` is planned for Phase 8 (local backup).
        console.print(
            "\n[yellow]The package was upgraded but migrations failed.[/yellow]\n"
            "If you have a backup, run [bold]dango restore <path>[/bold] to roll back.\n"
            "Otherwise, try running [bold]dango migrate run[/bold] manually."
        )
        raise SystemExit(1) from exc

    # Success
    console.print()
    console.print(
        f"[green]Upgrade complete:[/green] "
        f"[cyan]{current_version}[/cyan] → [cyan]{target_version}[/cyan]"
    )
    console.print()
    console.print("Restart services with [bold]dango start[/bold] to use the new version.")
