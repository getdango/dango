"""tests/unit/test_dlt_state_cleanup_edges.py

Edges of the local dlt state cleanup: a state directory stays when its warehouse state table could not be
dropped, a name shaped like a state backup is never followed, and a source with its own dataset name keeps
its cursor while that dataset holds data. Real dlt pipelines against a local DuckDB under a throwaway HOME.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from dango.ingestion.sources.setup_lifecycle import remove_source
from tests.factories.dlt_state_projects import (
    db_clean,
    entry,
    isolate_home,
    pipeline_dirs,
    pipelines_dir,
    plant_orphan_state,
    project,
    run_sync,
    write_sources,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


class _FailingDrop:
    """A DuckDB connection whose DROP of one table raises; everything else is the real thing."""

    def __init__(self, real: Any, table: str) -> None:
        self._real, self._table = real, table

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.startswith("DROP TABLE") and self._table in sql:
            raise duckdb.Error("cannot drop")
        return self._real.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def test_the_dir_stays_when_the_state_table_could_not_be_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("gone"))
    run_sync(root, "native", "gone", 5)
    write_sources(root)
    real_connect = duckdb.connect

    def connect(*args: Any, **kwargs: Any) -> Any:
        conn = real_connect(*args, **kwargs)
        return conn if kwargs.get("read_only") else _FailingDrop(conn, "_dlt_pipeline_state")

    monkeypatch.setattr(duckdb, "connect", connect)
    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert pipeline_dirs() == ["gone"]
    assert "Cleared the local sync state" not in result.output


def test_a_name_shaped_like_a_state_backup_is_never_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("kept"))
    run_sync(root, "native", "kept", 5)
    backup = pipelines_dir() / "kept_backup_20260101_120000"
    backup.mkdir()
    # Created in this project folder, so only the name keeps it from being removed.
    state = {"_local": {"initial_cwd": str(root)}}
    (backup / "state.json").write_text(json.dumps(state), encoding="utf-8")
    plant_orphan_state(root, "raw_evil", backup.name)

    assert db_clean(root, monkeypatch).exit_code == 0
    assert (backup / "state.json").exists()


def test_a_source_with_its_own_dataset_keeps_its_cursor_while_that_dataset_has_data(
    tmp_path: Path,
) -> None:
    native = {"dataset_name": "custom_ds"}
    root = project(tmp_path, "p1", entry("b", **native))
    assert run_sync(root, "native", "b", 5, native=native)["status"] == "success"

    remove_source(root, "b", force=True)
    assert pipeline_dirs() == ["b"]

    write_sources(root, entry("b", **native))
    assert run_sync(root, "native", "b", 8, native=native)["status"] == "success"
    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
    try:
        found = sorted(r[0] for r in con.execute("SELECT id FROM custom_ds.events").fetchall())
    finally:
        con.close()
    assert found == list(range(1, 9))
