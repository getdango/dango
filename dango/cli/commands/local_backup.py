"""dango/cli/commands/local_backup.py

Local backup management for Dango projects.

Command hierarchy::

    dango backup                    — List local backup archives
    dango backup restore FILE       — Restore from a local backup archive
"""

from __future__ import annotations

import tarfile
import time
from pathlib import Path

import click

from dango.cli import console
from dango.cli.utils import require_project_context, safe_confirm
from dango.platform.cloud.backup import BACKUP_DIRS, BACKUP_FILES

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SRV_PREFIX = "/srv/dango/project/"


def _create_safety_backup(project_root: Path) -> Path | None:
    """Create a safety backup before restore. Returns path or None on failure."""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = project_root / ".dango" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    safety_path = backup_dir / f"pre-restore-{timestamp}.tar.gz"

    try:
        with tarfile.open(safety_path, mode="w:gz") as tf:
            for fpath in BACKUP_FILES:
                src = project_root / fpath
                if src.exists():
                    tf.add(str(src), arcname=fpath)
            for dpath in BACKUP_DIRS:
                src = project_root / dpath
                if src.exists():
                    tf.add(str(src), arcname=dpath)
        return safety_path
    except (OSError, tarfile.TarError) as exc:
        console.print(f"[yellow]Warning:[/yellow] Could not create safety backup: {exc}")
        return None


def _extract_archive(archive_path: Path, project_root: Path) -> None:
    """Extract backup archive, mapping server paths to local project paths.

    Metabase H2 files (metabase/) are skipped — they are Docker volume paths
    not relevant locally.
    """
    with tarfile.open(archive_path, mode="r:gz") as tf:
        for member in tf.getmembers():
            # Skip Metabase H2 files (Docker volume data)
            if member.name.startswith("metabase/"):
                continue
            # Strip server prefix and map to project root
            if member.name.startswith(_SRV_PREFIX):
                rel_path = member.name[len(_SRV_PREFIX) :]
            elif member.name.startswith("backup-") and "/" in member.name:
                # Legacy path: backup-TIMESTAMP/project/...
                parts = member.name.split("/", 1)
                rel_path = parts[1] if len(parts) > 1 else member.name
            else:
                rel_path = member.name
            dest = project_root / rel_path
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile() or member.issym():
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Extract to file
                with tf.extractfile(member) as src_f:
                    dest.write_bytes(src_f.read())


# ---------------------------------------------------------------------------
# backup group
# ---------------------------------------------------------------------------


@click.group("backup", invoke_without_command=True)
@click.pass_context
def backup_group(ctx: click.Context) -> None:
    """Manage local backup archives.

    Without a subcommand, lists backups in .dango/backups/.

    Commands:
      restore    Restore from a local backup archive
    """
    if ctx.invoked_subcommand is not None:
        return

    project_root = require_project_context(ctx)
    backup_dir = project_root / ".dango" / "backups"

    if not backup_dir.exists():
        console.print("[yellow]No backups found.[/yellow]")
        console.print(
            "Backups are stored in [bold].dango/backups/[/bold]. "
            "Use [bold]dango remote backup[/bold] for cloud server backups."
        )
        return

    archives = sorted(
        backup_dir.glob("*.tar.gz"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not archives:
        console.print("[yellow]No backups found.[/yellow]")
        return

    console.print(f"[bold]Local backups ({len(archives)}):[/bold]")
    for archive in archives:
        size_mb = archive.stat().st_size / (1024 * 1024)
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(archive.stat().st_mtime))
        console.print(f"  {archive.name} — {size_mb:.1f} MB ({mtime})")


# ---------------------------------------------------------------------------
# backup restore
# ---------------------------------------------------------------------------


@backup_group.command("restore")
@click.argument("file", type=click.Path(exists=True))
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def backup_restore(ctx: click.Context, file: str, yes: bool) -> None:
    """Restore project data from a local backup archive.

    FILE is the path to a .tar.gz backup archive.  Current project data
    will be overwritten.  A safety backup is created before restoring.

    Examples:
      dango backup restore ./backup-20260224-143000.tar.gz
      dango backup restore ./backup-20260224-143000.tar.gz --yes
    """
    project_root = require_project_context(ctx)
    archive_path = Path(file).resolve()

    # Validate .tar.gz extension
    if not archive_path.name.endswith(".tar.gz"):
        console.print("[red]Error:[/red] Backup file must be a .tar.gz archive.")
        raise click.Abort()

    if not yes:
        if not safe_confirm(
            f"This will restore project data from '{archive_path.name}'. "
            "Current data will be overwritten. Continue?"
        ):
            console.print("[yellow]Restore cancelled.[/yellow]")
            return

    # Create safety backup
    console.print("Creating safety backup...")
    safety_path = _create_safety_backup(project_root)

    if safety_path is None and not yes:
        console.print(
            "[yellow]Safety backup could not be created.[/yellow] "
            "It is recommended to manually back up your data before proceeding."
        )
        if not safe_confirm("Continue without safety backup?"):
            console.print("[yellow]Restore cancelled.[/yellow]")
            return
    elif safety_path is None:
        console.print("[yellow]Warning:[/yellow] Safety backup skipped, continuing without it.")
    else:
        console.print(f"[green]Safety backup created:[/green] {safety_path.name}")

    # Extract archive
    console.print(f"Restoring from {archive_path.name}...")
    try:
        _extract_archive(archive_path, project_root)
    except (OSError, tarfile.TarError) as exc:
        console.print(f"[red]Error:[/red] Restore failed: {exc}")
        if safety_path and safety_path.exists():
            console.print(
                f"[yellow]You can recover from the safety backup: "
                f"dango backup restore {safety_path} --yes[/yellow]"
            )
        raise click.Abort() from exc

    console.print("[green]Restore complete.[/green]")
    if safety_path and safety_path.exists():
        console.print(f"  Safety backup: {safety_path.name}")
