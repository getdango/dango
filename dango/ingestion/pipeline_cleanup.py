"""dango/ingestion/pipeline_cleanup.py

Ends the local dlt pipeline state (incremental cursor, kept packages) of a source whose data is gone.
Used by `dango source remove` and `dango db clean`. Local code: runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any, NamedTuple

from dango.config.models import DataSource
from dango.config.source_datasets import dataset_name_of, pipeline_name_of, schemas_of
from dango.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "StateDrop",
    "clear_state_of_removed_source",
    "dataset_name_of",
    "drop_local_pipeline_state",
    "made_by_project",
    "names_to_clear",
    "read_state_names",
    "state_note",
    "will_be_removed",
    "pipeline_name_of",
    "pipeline_names_in_state_table",
    "pipeline_names_in_use",
    "warehouse_has_data_for",
]


def pipeline_names_in_use(sources: Iterable[DataSource]) -> set[str]:
    """Pipeline names of the dlt-backed sources in *sources*, case-folded.

    Compare with ``name.casefold()``: on macOS and Windows ``KEPT`` and ``kept`` are one directory.
    """
    return {name.casefold() for name in map(pipeline_name_of, sources) if name}


def warehouse_has_data_for(duckdb_path: Path, source: DataSource) -> bool:
    """True when the warehouse still holds any table of a schema the source may write to.

    Conservative: a warehouse that cannot be read (locked, corrupt) counts as holding data, so the
    cursor is kept. A missing warehouse file holds none.
    """
    if not duckdb_path.exists():
        return False
    try:
        import duckdb

        con = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            first, second = (schemas_of(source) * 2)[:2]  # one or two candidate schemas
            row = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables "
                "WHERE lower(table_schema) IN (?, ?)",
                [first, second],
            ).fetchone()
        finally:
            con.close()
    except Exception:
        return True
    return bool(row and row[0])


def pipeline_names_in_state_table(conn: Any, schema: str) -> set[str]:
    """Pipeline names recorded in ``<schema>._dlt_pipeline_state`` (empty when unreadable)."""
    qualified = '"' + schema.replace('"', '""') + '"._dlt_pipeline_state'
    try:
        rows = conn.execute(
            "SELECT DISTINCT pipeline_name FROM query_table(?)", [qualified]
        ).fetchall()
    except Exception:
        return set()
    return {str(row[0]) for row in rows if row[0]}


class StateDrop(NamedTuple):
    """What happened to one pipeline directory: removed, absent, foreign (kept) or failed."""

    status: str
    error: str = ""


def made_by_project(pipeline_dir: Path, project_root: Path) -> bool:
    """True only when ``state.json`` says the directory was created in *project_root*.

    dlt records the working directory of the process that created the pipeline in
    ``_local.initial_cwd``; Dango creates its pipelines from the project root. The two folders are
    compared with ``os.path.samefile`` (symlinks, case and trailing separators on macOS and Windows,
    drive letters), which needs both to exist: a project folder that was moved or deleted since is
    "not this one". A missing or unreadable file, or anything unexpected in it, is False.
    """
    try:
        text = (pipeline_dir / "state.json").read_text(encoding="utf-8")
        cwd = json.loads(text)["_local"]["initial_cwd"]
        return isinstance(cwd, str) and bool(cwd) and os.path.samefile(cwd, project_root)
    except Exception:
        return False


def will_be_removed(pipeline_name: str, project_root: Path) -> bool:
    """True when ``drop_local_pipeline_state`` would remove the directory now."""
    try:
        from dango.ingestion.dlt_runner import _checked_pipeline_dir

        path = _checked_pipeline_dir(pipeline_name)
        return path.is_dir() and made_by_project(path, project_root)
    except Exception:
        return False


def drop_local_pipeline_state(pipeline_name: str, project_root: Path) -> StateDrop:
    """Remove ``<pipelines dir>/<pipeline_name>`` (the local state of one pipeline).

    Only that exact directory, only for a name that is a single path component, and only when it
    was created in *project_root* (another project's cursor is never deleted: ``foreign``). Best
    effort: never raises; ``failed`` carries the error type. ``DLT_DATA_DIR`` decides the pipelines
    dir, as for every sync.
    """
    try:
        from dango.ingestion.dlt_runner import _checked_pipeline_dir

        path = _checked_pipeline_dir(pipeline_name)
        if not path.is_dir():
            return StateDrop("absent")
        if not made_by_project(path, project_root):
            return StateDrop("foreign")
        shutil.rmtree(path)
        return StateDrop("removed")
    except ValueError:
        return StateDrop("absent")
    except Exception as exc:
        logger.warning("local_pipeline_state_not_removed", error_type=type(exc).__name__)
        return StateDrop("failed", type(exc).__name__)


def state_note(pipeline_name: str, drop: StateDrop) -> str | None:
    """The one console line for *drop* (plain ASCII; None when there is nothing to say)."""
    again = f"If '{pipeline_name}' is added again and loads no rows, run: "
    command = f"dango sync {pipeline_name} --full-refresh"
    if drop.status == "removed":
        return f"Cleared the local sync state for '{pipeline_name}'"
    if drop.status == "foreign":
        return (
            f"Kept the local sync state for '{pipeline_name}': it was created in another "
            f"folder. {again}{command}"
        )
    if drop.status == "failed":
        return (
            f"Could not clear the local sync state for '{pipeline_name}' ({drop.error}); "
            f"{again[0].lower()}{again[1:]}{command}"
        )
    return None


def read_state_names(duckdb_path: Path, schemas: Iterable[str]) -> dict[str, set[str]]:
    """Pipeline names per schema from ``<schema>._dlt_pipeline_state`` (read-only; {} if unreadable)."""
    wanted = list(schemas)
    if not wanted:
        return {}
    try:
        import duckdb

        con = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            return {schema: pipeline_names_in_state_table(con, schema) for schema in wanted}
        finally:
            con.close()
    except Exception:
        return {}


def names_to_clear(
    names_by_schema: dict[str, set[str]], schemas: Iterable[str], sources: Iterable[DataSource]
) -> list[str]:
    """Pipeline names of the dropped state tables of *schemas*, minus names a source still uses.

    A schema whose state table named nothing falls back to the source name in ``raw_<name>``.
    """
    names: set[str] = set()
    for schema in schemas:
        names |= names_by_schema.get(schema, set()) or {schema.removeprefix("raw_")}
    in_use = pipeline_names_in_use(sources)
    return sorted(name for name in names if name.casefold() not in in_use)


def clear_state_of_removed_source(
    project_root: Path, source: DataSource, others: list[Any], result: Any
) -> None:
    """End a removed source's local dlt state when the warehouse holds no data for it.

    While its tables remain (``dango db clean`` has not run) the cursor stays: a source added again
    must continue from it, or every row would load a second time. A pipeline name that one of the
    *others* (the sources that remain) still uses stays too, and so does a directory created in
    another project folder. Sets ``result.pipeline_state_cleared`` and, when the directory stayed or
    could not be removed, appends one line to ``result.warnings``. Never raises.
    """
    try:
        name = pipeline_name_of(source)
        if name is None or name.casefold() in pipeline_names_in_use(others):
            return
        if warehouse_has_data_for(project_root / "data" / "warehouse.duckdb", source):
            return
        drop = drop_local_pipeline_state(name, project_root)
        result.pipeline_state_cleared = drop.status == "removed"
        note = state_note(name, drop)
        if note and not result.pipeline_state_cleared:
            result.warnings.append(note)
    except Exception:
        return
