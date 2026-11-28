"""dango/ingestion/pipeline_cleanup.py

Ends the local dlt pipeline state (incremental cursor, kept packages) of a source whose data is gone.
Used by `dango source remove` and `dango db clean`. Local code: runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dango.config.models import DataSource, SourceType
from dango.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "clear_state_of_removed_source",
    "dataset_name_of",
    "drop_local_pipeline_state",
    "pipeline_name_of",
    "pipeline_names_in_state_table",
    "pipeline_names_in_use",
    "warehouse_has_data_for",
]

_FILE_SOURCES = (SourceType.CSV, SourceType.LOCAL_FILES)


def pipeline_name_of(source: DataSource) -> str | None:
    """The dlt pipeline name the runner uses for *source* (None for file sources: no dlt state)."""
    if source.type in _FILE_SOURCES:
        return None
    native = getattr(source, "dlt_native", None)
    override = (
        getattr(native, "pipeline_name", None) if source.type == SourceType.DLT_NATIVE else None
    )
    return str(override or source.name)


def dataset_name_of(source: DataSource) -> str:
    """The warehouse schema the runner loads *source* into."""
    native = getattr(source, "dlt_native", None)
    override = (
        getattr(native, "dataset_name", None) if source.type == SourceType.DLT_NATIVE else None
    )
    return str(override or f"raw_{source.name}")


def pipeline_names_in_use(sources: Iterable[DataSource]) -> set[str]:
    """Pipeline names of the dlt-backed sources in *sources*."""
    return {name for name in map(pipeline_name_of, sources) if name}


def warehouse_has_data_for(duckdb_path: Path, source: DataSource) -> bool:
    """True when the warehouse still holds any table of the source's schema.

    Conservative: a warehouse that cannot be read (locked, corrupt) counts as holding data, so the
    cursor is kept. A missing warehouse file holds none.
    """
    if not duckdb_path.exists():
        return False
    try:
        import duckdb

        con = duckdb.connect(str(duckdb_path), read_only=True)
        try:
            row = con.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = ?",
                [dataset_name_of(source)],
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


def drop_local_pipeline_state(pipeline_name: str) -> bool:
    """Remove ``<pipelines dir>/<pipeline_name>`` (the local state of one pipeline).

    Only that exact directory, and only for a name that is a single path component. Best effort:
    returns False, never raises, when the directory is absent, the name is unsafe or the removal
    fails. ``DLT_DATA_DIR`` decides the pipelines dir, as for every sync.
    """
    try:
        from dango.ingestion.dlt_runner import _checked_pipeline_dir

        path = _checked_pipeline_dir(pipeline_name)
        if not path.is_dir():
            return False
        shutil.rmtree(path)
        return True
    except ValueError:
        return False
    except Exception as exc:
        logger.warning("local_pipeline_state_not_removed", error_type=type(exc).__name__)
        return False


def clear_state_of_removed_source(
    project_root: Path, source: DataSource, others: list[Any]
) -> bool:
    """End a removed source's local dlt state when the warehouse holds no data for it.

    While its tables remain (``dango db clean`` has not run) the cursor stays: a source added again
    must continue from it, or every row would load a second time. A pipeline name that one of the
    *others* (the sources that remain) still uses stays too. Never raises.
    """
    try:
        name = pipeline_name_of(source)
        if name is None or name in pipeline_names_in_use(others):
            return False
        if warehouse_has_data_for(project_root / "data" / "warehouse.duckdb", source):
            return False
        return drop_local_pipeline_state(name)
    except Exception:
        return False
