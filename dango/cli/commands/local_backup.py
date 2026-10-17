"""dango/cli/commands/local_backup.py

Local backup management for Dango projects.

Command hierarchy::

    dango backup                    — List local backup archives
    dango backup restore FILE       — Restore from a local backup archive
"""

from __future__ import annotations

import os
import shutil
import tarfile
import tempfile
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import click
from rich.markup import escape

from dango.cli import console
from dango.cli.utils import require_project_context, safe_confirm
from dango.platform.cloud.backup import (
    BACKUP_DIRS,
    BACKUP_FILES,
    DATABASE_FILES,
    METABASE_CONFIG_FILE,
    _sanitized_metabase_yaml,
    _sanitized_metabase_yaml_text,
    wal_sidecar_names,
)
from dango.platform.cloud.backup_restore import (
    AUTH_CONFIG_FILE,
    AUTH_CONFIG_SKIPPED_WARNING,
    auth_config_keeps_auth_on,
    is_backup_excluded,
    unsafe_tar_members,
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


def _add_tree(tf: tarfile.TarFile, root: Path, arcname: str) -> None:
    """Add regular files and directories below *root* as *arcname*/…, never following links.

    ``BACKUP_EXCLUDES`` names are left out; links and special files are skipped with a
    warning (a restore refuses archives that contain them).
    """
    tf.add(str(root), arcname=arcname, recursive=False)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = arcname if rel_dir == "." else f"{arcname}/{rel_dir}"
        kept_dirs = []
        for name in sorted(dirnames):
            path = Path(dirpath) / name
            if is_backup_excluded(name):
                continue
            if path.is_symlink():
                console.print(
                    f"[yellow]Warning:[/yellow] Not in safety backup (link): {escape(f'{prefix}/{name}')}"
                )
                continue
            tf.add(str(path), arcname=f"{prefix}/{name}", recursive=False)
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if is_backup_excluded(name):
                continue
            if path.is_symlink() or not path.is_file():
                console.print(
                    "[yellow]Warning:[/yellow] Not in safety backup (link or special file): "
                    f"{escape(f'{prefix}/{name}')}"
                )
                continue
            tf.add(str(path), arcname=f"{prefix}/{name}", recursive=False)


def _create_safety_backup(project_root: Path) -> Path | None:
    """Create a safety backup before restore. Returns path or None on failure.

    The name is unique (two restores in the same second keep separate archives).
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = project_root / ".dango" / "backups"
    safety_path: Path | None = None

    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(
            prefix=f"pre-restore-{timestamp}-", suffix=".tar.gz", dir=backup_dir
        )
        safety_path = Path(name)
        with os.fdopen(fd, "wb") as handle, tarfile.open(fileobj=handle, mode="w:gz") as tf:
            # Committed writes may still live only in a WAL, which the restore
            # deletes: fold it in, or keep the WAL in the archive.
            carried: list[str] = []
            for db_path in DATABASE_FILES:
                sidecars = _live_sidecars(project_root, db_path)
                if (project_root / db_path).is_file() and sidecars:
                    if not _checkpoint_database(project_root / db_path):
                        console.print(
                            f"[yellow]Warning:[/yellow] Could not checkpoint {db_path}; "
                            "its WAL files are included in the safety backup."
                        )
                        carried.extend(sidecars)
            for fpath in BACKUP_FILES:
                src = project_root / fpath
                if src.is_file():
                    tf.add(os.path.realpath(src), arcname=fpath)
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
                if src.is_dir():
                    _add_tree(tf, src.resolve(), dpath)
            for sidecar in carried:
                tf.add(os.path.realpath(project_root / sidecar), arcname=sidecar)
        return safety_path
    except (OSError, tarfile.TarError) as exc:
        if safety_path is not None:
            safety_path.unlink(missing_ok=True)
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


def _member_rel_path(name: str) -> str:
    """Map an archive member name to a path relative to the project root."""
    if name.startswith(_SRV_PREFIX):
        return name[len(_SRV_PREFIX) :]
    if name.startswith("backup-") and "/" in name:
        # Legacy path: backup-TIMESTAMP/project/...
        return name.split("/", 1)[1]
    return name


_DB_SIDECARS = frozenset(side for db in DATABASE_FILES for side in wal_sidecar_names(db))


def _is_backup_member(rel_path: str) -> bool:
    """True if *rel_path* is something a Dango backup contains (secrets never are)."""
    if rel_path in ("manifest.json", METABASE_CONFIG_FILE) or rel_path in BACKUP_FILES:
        return True
    if rel_path in _DB_SIDECARS:  # carried by a safety backup when a checkpoint failed
        return True
    return any(rel_path.startswith(d + "/") for d in BACKUP_DIRS)


def _write_streamed(src_f: Any, dest: Path, before_replace: Any) -> None:
    """Stream *src_f* to ``<dest>.restore-tmp`` in chunks, then atomically replace *dest*.

    *before_replace* runs after the copy succeeded, just before the rename.  On any
    failure the temporary file is removed and *dest* is left as it was.
    """
    tmp = dest.with_name(dest.name + ".restore-tmp")
    try:
        with open(tmp, "wb") as out:
            shutil.copyfileobj(src_f, out, 1024 * 1024)
        before_replace()
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _extract_archive(
    archive_path: Path, project_root: Path, *, keep_wal_suffix: str = ""
) -> list[str]:
    """Extract backup archive, mapping server paths to local project paths.

    Only backup members are written (``BACKUP_FILES``, files under ``BACKUP_DIRS``,
    the manifest and Metabase metadata); anything else, including ``.env`` and
    ``.dlt/secrets.toml``, is skipped and listed.  Metabase H2 files (metabase/) are
    skipped — they are Docker volume paths not relevant locally.  On a cloud server an
    ``auth.yml`` that would turn authentication off is not restored.  Each file is
    streamed to a temporary file and renamed into place.  Paths that resolve outside
    *project_root* (path traversal, or a symlink pointing elsewhere) are not written;
    their relative paths are returned so the caller can report an incomplete restore.
    """
    from dango.config.helpers import is_running_on_cloud

    on_server = is_running_on_cloud()
    resolved_root = project_root.resolve()
    skipped: list[str] = []
    rejected: list[str] = []
    with tarfile.open(archive_path, mode="r:gz") as tf:
        # Skip Metabase H2 files (Docker volume data), whatever the archive's top-level dir.
        members = [
            m
            for m in tf.getmembers()
            if _member_rel_path(m.name).rstrip("/").split("/", 1)[0] != "metabase" and m.isfile()
        ]
        # Where each member lands; a WAL carried by the archive (safety backup) is kept.
        carried = {(project_root / _member_rel_path(m.name)).resolve() for m in members}
        for member in members:
            rel_path = _member_rel_path(member.name)
            if not _is_backup_member(rel_path):
                rejected.append(rel_path)
                continue
            dest = (project_root / rel_path).resolve()
            # Defend against path traversal (absolute paths, .. components, outside symlinks)
            if not str(dest).startswith(str(resolved_root) + "/") and dest != resolved_root:
                skipped.append(rel_path)
                continue
            src_f = tf.extractfile(member)
            if src_f is None:
                continue
            if rel_path in (METABASE_CONFIG_FILE, AUTH_CONFIG_FILE):
                text = src_f.read(1024 * 1024).decode(errors="replace")
                if rel_path == AUTH_CONFIG_FILE and on_server:
                    if not auth_config_keeps_auth_on(text):
                        console.print(f"[yellow]Warning:[/yellow] {AUTH_CONFIG_SKIPPED_WARNING}")
                        continue
                if rel_path == METABASE_CONFIG_FILE:
                    try:
                        text = _sanitized_metabase_yaml_text(text)
                    except ValueError:
                        console.print(
                            "[yellow]Warning:[/yellow] Metabase metadata omitted because its "
                            "YAML could not be safely sanitized."
                        )
                        continue
                src_f = BytesIO(text.encode())

            def clear_sidecars(rel: str = rel_path, target: Path = dest) -> None:
                """Clear the live WAL sidecars of the database being replaced."""
                # A leftover live WAL would be replayed into the restored database.
                # The database type comes from its logical name; the sidecars sit
                # next to the file actually written (which may be a symlink target).
                for side_rel in wal_sidecar_names(rel):
                    side = Path(str(target) + side_rel[len(rel) :])
                    if side not in carried:
                        _clear_sidecar(side, keep_wal_suffix)

            dest.parent.mkdir(parents=True, exist_ok=True)
            _write_streamed(src_f, dest, clear_sidecars)
    if rejected:
        console.print(
            "[yellow]Warning:[/yellow] Skipped archive entries that are not part of a Dango "
            "backup (secrets such as .env are never restored):"
        )
        for rel_path in rejected:
            console.print(f"  {escape(rel_path)}")
    return skipped


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

    # Links and special files are never restored: refuse before changing anything.
    try:
        unsafe = unsafe_tar_members(archive_path)
    except (OSError, tarfile.TarError) as exc:
        console.print(f"[red]Error:[/red] Cannot read backup archive: {exc}")
        raise click.Abort() from exc
    if unsafe:
        console.print(
            "[red]Error:[/red] Archive contains links or special files, which a restore "
            f"never writes: {', '.join(unsafe)}. Nothing was restored."
        )
        raise click.Abort()

    # Never overwrite files a running Dango (web server, sync, dbt) has open.
    from dango.cli.helpers.process_manager import is_project_server_running

    stop_first = (
        "Stop Dango first (`dango stop`), then run the restore again. Nothing was restored."
    )
    if is_project_server_running(project_root):
        console.print(f"[red]Error:[/red] Dango is running for this project. {escape(stop_first)}")
        raise click.Abort()

    from dango.exceptions import DbtLockError
    from dango.utils.dbt_lock import dbt_lock

    try:
        with dbt_lock(project_root, source="cli", operation="restore", timeout=5):
            _restore_locked(project_root, archive_path, yes)
    except DbtLockError as exc:
        console.print(
            "[red]Error:[/red] A sync or dbt run is in progress for this project. "
            f"{escape(stop_first)}"
        )
        raise click.Abort() from exc


def _restore_locked(project_root: Path, archive_path: Path, yes: bool) -> None:
    """Confirm, take a safety backup and extract *archive_path* (caller holds the dbt lock)."""
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
        skipped = _extract_archive(archive_path, project_root, keep_wal_suffix=keep)
    except (OSError, tarfile.TarError) as exc:
        console.print(f"[red]Error:[/red] Restore failed: {exc}")
        if safety_path and safety_path.exists():
            console.print(
                f"[yellow]You can recover from the safety backup: "
                f"dango backup restore {safety_path} --yes[/yellow]"
            )
        raise click.Abort() from exc

    if skipped:
        console.print(
            "[red]Restore incomplete:[/red] these files were not restored because their "
            "destination is outside the project (for example a symlink pointing elsewhere):"
        )
        for rel_path in skipped:
            console.print(f"  {rel_path}")
        if safety_path and safety_path.exists():
            console.print(f"  Safety backup: {safety_path.name}")
        raise click.Abort()

    console.print("[green]Restore complete.[/green]")
    if safety_path and safety_path.exists():
        console.print(f"  Safety backup: {safety_path.name}")
