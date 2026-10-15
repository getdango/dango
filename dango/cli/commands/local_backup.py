"""dango/cli/commands/local_backup.py

Local backup management for Dango projects.

Command hierarchy::

    dango backup                    — List local backup archives
    dango backup restore FILE       — Restore from a local backup archive
"""

from __future__ import annotations

import tarfile
import time
from io import BytesIO
from pathlib import Path

import click

from dango.cli import console
from dango.cli.utils import require_project_context, safe_confirm
from dango.platform.cloud.backup import (
    BACKUP_DIRS,
    BACKUP_FILES,
    METABASE_CONFIG_FILE,
    _sanitized_metabase_yaml,
    _sanitized_metabase_yaml_text,
    wal_sidecar_names,
)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SRV_PREFIX = "/srv/dango/project/"


def _checkpoint_database(db_path: Path) -> bool:
    """Fold a database's WAL into its main file.  Returns False if that is not possible."""
    import sqlite3

    import duckdb

    try:
        if db_path.suffix == ".duckdb":
            duck = duckdb.connect(str(db_path))
            try:
                duck.execute("CHECKPOINT")
            finally:
                duck.close()
            return True
        lite = sqlite3.connect(str(db_path))
        try:
            busy = lite.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            lite.close()
        return busy is not None and busy[0] == 0
    except (duckdb.Error, sqlite3.Error, OSError):
        return False


def _live_sidecars(project_root: Path, fpath: str) -> list[str]:
    """Return the relative WAL sidecar paths of *fpath* that exist (non-empty) in the project."""
    return [
        s
        for s in wal_sidecar_names(fpath)
        if (project_root / s).is_file() and (project_root / s).stat().st_size > 0
    ]


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
                    # Committed writes may still live only in the WAL, which the
                    # restore deletes: fold it in, or keep the WAL in the archive.
                    sidecars = _live_sidecars(project_root, fpath)
                    if sidecars and not _checkpoint_database(src):
                        console.print(
                            f"[yellow]Warning:[/yellow] Could not checkpoint {fpath}; "
                            "its WAL files are included in the safety backup."
                        )
                    else:
                        sidecars = []
                    tf.add(str(src), arcname=fpath)
                    for sidecar in sidecars:
                        tf.add(str(project_root / sidecar), arcname=sidecar)
            metabase_source = project_root / METABASE_CONFIG_FILE
            if metabase_source.exists():
                try:
                    sanitized = _sanitized_metabase_yaml(metabase_source).encode()
                    info = tarfile.TarInfo(METABASE_CONFIG_FILE)
                    info.size = len(sanitized)
                    tf.addfile(info, BytesIO(sanitized))
                except ValueError:
                    console.print(
                        "[yellow]Warning:[/yellow] Metabase metadata omitted because its YAML "
                        "could not be safely sanitized."
                    )
            for dpath in BACKUP_DIRS:
                src = project_root / dpath
                if src.exists():
                    tf.add(str(src), arcname=dpath)
        return safety_path
    except (OSError, tarfile.TarError) as exc:
        console.print(f"[yellow]Warning:[/yellow] Could not create safety backup: {exc}")
        return None


def _clear_sidecar(sidecar: Path, keep_suffix: str) -> None:
    """Delete a live WAL sidecar, or rename it with *keep_suffix* when one is given."""
    if not keep_suffix:
        sidecar.unlink(missing_ok=True)
    elif sidecar.exists():
        kept = sidecar.with_name(sidecar.name + keep_suffix)
        sidecar.replace(kept)
        console.print(f"[yellow]Warning:[/yellow] Live database WAL kept as {kept}")


def _extract_archive(archive_path: Path, project_root: Path, *, keep_wal_suffix: str = "") -> None:
    """Extract backup archive, mapping server paths to local project paths.

    Metabase H2 files (metabase/) are skipped — they are Docker volume paths
    not relevant locally.  Paths that escape *project_root* are silently
    skipped to prevent path-traversal attacks.
    """
    resolved_root = project_root.resolve()
    with tarfile.open(archive_path, mode="r:gz") as tf:
        members = tf.getmembers()
        member_names = {m.name for m in members}
        for member in members:
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
            dest = (project_root / rel_path).resolve()
            # Defend against path traversal (absolute paths, .. components)
            if not str(dest).startswith(str(resolved_root) + "/") and dest != resolved_root:
                continue
            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif member.isfile() or member.issym():
                dest.parent.mkdir(parents=True, exist_ok=True)
                # Extract to file
                src_f = tf.extractfile(member)
                if src_f is not None:
                    content = src_f.read()
                    if rel_path == METABASE_CONFIG_FILE:
                        try:
                            content = _sanitized_metabase_yaml_text(content.decode()).encode()
                        except (UnicodeDecodeError, ValueError):
                            console.print(
                                "[yellow]Warning:[/yellow] Metabase metadata omitted because its "
                                "YAML could not be safely sanitized."
                            )
                            continue
                    # A leftover live WAL would be replayed into the restored database,
                    # unless the archive carries that WAL itself (a safety backup).
                    prefix = member.name[: len(member.name) - len(rel_path)]
                    for side_rel, side in zip(
                        wal_sidecar_names(rel_path), wal_sidecar_names(str(dest)), strict=True
                    ):
                        if prefix + side_rel not in member_names:
                            _clear_sidecar(Path(side), keep_wal_suffix)
                    dest.write_bytes(content)


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
        # Without a safety backup, a live WAL may hold the only copy of recent writes.
        keep = "" if safety_path else f".pre-restore-{time.strftime('%Y%m%d-%H%M%S')}"
        _extract_archive(archive_path, project_root, keep_wal_suffix=keep)
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
