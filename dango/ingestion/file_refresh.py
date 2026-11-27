"""dango/ingestion/file_refresh.py

The full-refresh window of a csv / local_files sync: back the table and file metadata up, and put
them back when the refresh does not finish.

``dlt_runner`` runs one window per ``--full-refresh``. The live table is renamed to
``<name>_pre_refresh_backup`` and the source's ``_dango_file_metadata`` rows are cleared, so the
files load as new. A successful load drops the backup; an exception, an interrupt (Ctrl+C or
SIGTERM) or an empty result restores it. Level 1 module; local code, runs on Windows, macOS and
Linux.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
from rich.console import Console

from dango.ingestion.interrupts import is_interrupt, reraise_if_interrupt
from dango.utils.refresh_backup import refresh_backup_table_name
from dango.validation import quote_identifier

console = Console()
logger = logging.getLogger(__name__)

_METADATA_INSERT = (
    "INSERT INTO _dango_file_metadata "
    "(source_name, file_path, file_size, file_mtime, "
    "rows_loaded, status, loaded_at, error_message) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


@dataclass
class RefreshState:
    """What a full refresh has changed so far (filled in step by step, so an interrupt at any
    point still knows what to put back)."""

    has_backup: bool = False
    metadata_backup: list[tuple] = field(default_factory=list)


def _connect(duckdb_path: Path, source_name: str, operation: str) -> duckdb.DuckDBPyConnection:
    # Lazy import: dlt_runner imports this module at its own top level
    from dango.ingestion.dlt_runner import _connect_with_lock_retry

    return _connect_with_lock_retry(duckdb_path, source_name, operation)


def begin_full_refresh(
    state: RefreshState,
    *,
    duckdb_path: Path,
    source_name: str,
    target_schema: str,
    label: str,
    pre_refresh_rows: int | None,
    allow_empty_replace: bool,
    rich_err: Callable[[Exception], str],
) -> None:
    """Back up (or drop) the existing table and clear the source's file metadata.

    Sets ``state.has_backup`` before the rename and ``state.metadata_backup`` before the rows are
    deleted, so :func:`finish_aborted_refresh` can undo whatever was done.
    """
    live = f"{quote_identifier(target_schema)}.{quote_identifier(source_name)}"
    conn = _connect(duckdb_path, source_name, f"{label}-full-refresh-drop")
    try:
        if pre_refresh_rows is not None and pre_refresh_rows > 0 and not allow_empty_replace:
            backup_name = refresh_backup_table_name(source_name)
            backup = f"{quote_identifier(target_schema)}.{quote_identifier(backup_name)}"
            # Drop stale backup if one exists from a previous crash
            conn.execute(f"DROP TABLE IF EXISTS {backup}")
            # Rename to backup instead of dropping; restored if the load does not finish
            state.has_backup = True
            try:
                conn.execute(f"ALTER TABLE {live} RENAME TO {quote_identifier(backup_name)}")
                console.print("  🔄 Full refresh: existing data backed up")
            except duckdb.CatalogException:
                # Table does not exist — nothing to protect
                state.has_backup = False
                console.print("  🔄 Full refresh: no existing table")
        else:
            # No data to protect — drop normally
            conn.execute(f"DROP TABLE IF EXISTS {live}")
            console.print("  🔄 Full refresh: dropped existing table")

        # Backup metadata rows before deleting (restored when the refresh does not finish)
        if state.has_backup:
            state.metadata_backup = conn.execute(
                "SELECT * FROM _dango_file_metadata WHERE source_name = ?",
                [source_name],
            ).fetchall()

        # Clear metadata so files are treated as new
        conn.execute("DELETE FROM _dango_file_metadata WHERE source_name = ?", [source_name])
        console.print("  🔄 Full refresh: cleared file metadata")
    except Exception as exc:
        reraise_if_interrupt(exc)
        console.print(f"  ⚠️  Could not drop table/metadata: {rich_err(exc)}")
    finally:
        conn.close()


def restore_refresh_backup(
    duckdb_path: Path,
    source_name: str,
    target_schema: str,
    metadata_backup: list[tuple],
    operation: str,
) -> bool:
    """Put the backed-up table and file metadata back; any partial live table is dropped.

    One transaction: either the previous table and metadata are back, or nothing changed. Returns
    True when the live table holds the previous data afterwards (also when no backup table exists,
    because then nothing was moved), False when no restore was possible. Errors propagate.
    """
    backup_name = refresh_backup_table_name(source_name)
    conn = _connect(duckdb_path, source_name, operation)
    try:
        backup_found = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = ? AND table_name = ?",
            [target_schema, backup_name],
        ).fetchall()
        if not backup_found:
            return True
        has_metadata_table = bool(
            conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = '_dango_file_metadata'"
            ).fetchall()
        )
        conn.execute("BEGIN TRANSACTION")
        try:
            conn.execute(
                f"DROP TABLE IF EXISTS {quote_identifier(target_schema)}.{quote_identifier(source_name)}"
            )
            conn.execute(
                f"ALTER TABLE {quote_identifier(target_schema)}.{quote_identifier(backup_name)} "
                f"RENAME TO {quote_identifier(source_name)}"
            )
            # Whatever the unfinished load recorded is replaced by the previous rows
            if has_metadata_table:
                conn.execute(
                    "DELETE FROM _dango_file_metadata WHERE source_name = ?", [source_name]
                )
                if metadata_backup:
                    conn.executemany(_METADATA_INSERT, metadata_backup)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        return True
    finally:
        conn.close()


def _restore_quietly(
    duckdb_path: Path,
    source_name: str,
    target_schema: str,
    state: RefreshState,
    operation: str,
) -> bool:
    """:func:`restore_refresh_backup` that never raises (so it cannot hide the original error)."""
    try:
        return restore_refresh_backup(
            duckdb_path, source_name, target_schema, state.metadata_backup, operation
        )
    except Exception as exc:
        # The type only: the message can carry paths or file names
        logger.warning(
            "file_refresh_restore_failed: source=%s error_type=%s", source_name, type(exc).__name__
        )
        console.print(
            f"  ⚠️  Could not restore the previous data automatically ({type(exc).__name__}); "
            f"it is kept in {target_schema}.{refresh_backup_table_name(source_name)}"
        )
        return False


def finish_aborted_refresh(
    exc: BaseException,
    state: RefreshState,
    *,
    duckdb_path: Path,
    source_name: str,
    target_schema: str,
    label: str,
) -> dict[str, Any] | None:
    """Undo a full refresh that ended by *exc* (any ``BaseException``), without hiding it.

    Restores the previous table and metadata when a backup was taken. Returns the ``interrupted``
    result when *exc* is an interrupt (Ctrl+C, SIGTERM, DuckDB's report of one); for any other
    exception returns ``None`` and the caller re-raises it.
    """
    restored = False
    if state.has_backup:
        restored = _restore_quietly(
            duckdb_path, source_name, target_schema, state, f"{label}-refresh-restore"
        )
    if not (isinstance(exc, KeyboardInterrupt) or is_interrupt(exc)):
        if restored:
            console.print("  [green]Full refresh did not finish: previous data restored[/green]")
        return None
    console.print("\n  [yellow]Sync interrupted[/yellow]")
    if restored:
        console.print("  [green]Previous data restored — run the same command again[/green]")
    elif not state.has_backup:
        console.print("  [green]Progress saved — resume with the same command[/green]")
    return {
        "status": "interrupted",
        "source": source_name,
        "rows_loaded": 0,
        "uses_replace_mode": True,
    }
