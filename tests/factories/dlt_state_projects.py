"""tests/factories/dlt_state_projects.py

Scratch projects on disk with `dlt_native` sources, real dlt pipelines against a local DuckDB under a
throwaway HOME (the local default pipelines dir, no DLT_DATA_DIR), and the CLI/service calls that remove
a source or clean the warehouse.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import dlt
import duckdb
import pytest
import yaml
from click.testing import CliRunner

from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.empty_guard_runs import sync
from tests.factories.refresh_cursor_runs import events

FIXTURE_MODULE = {"source_module": "fixture_module", "source_function": "make"}


def isolate_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Throwaway HOME with every telemetry opt-out; the local default pipelines dir (no DLT_DATA_DIR)."""
    (home / ".dango").mkdir(parents=True)
    (home / ".dango" / "config.yml").write_text(
        "telemetry: false\ndlt_telemetry: false\ndbt_telemetry: false\n", encoding="utf-8"
    )
    for key, value in {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "DO_NOT_TRACK": "1",
        "RUNTIME__DLTHUB_TELEMETRY": "false",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DANGO_TELEMETRY": "false",
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


def pipelines_dir() -> Path:
    from dango.ingestion.dlt_runner import _dlt_pipelines_dir

    return _dlt_pipelines_dir()


def entry(name: str, **native: Any) -> dict[str, Any]:
    return {"name": name, "type": "dlt_native", "dlt_native": {**FIXTURE_MODULE, **native}}


def write_sources(root: Path, *entries: dict[str, Any]) -> None:
    (root / ".dango").mkdir(parents=True, exist_ok=True)
    (root / ".dango" / "project.yml").write_text(
        yaml.safe_dump({"project": {"name": "P", "created_by": "t@example.com", "purpose": "t"}}),
        encoding="utf-8",
    )
    (root / ".dango" / "sources.yml").write_text(
        yaml.safe_dump({"version": "1.0", "sources": list(entries)}), encoding="utf-8"
    )


def project(tmp_path: Path, name: str, *entries: dict[str, Any]) -> Path:
    root = tmp_path / name
    write_sources(root, *entries)
    return root


def named_source(name: str, rows: int) -> Any:
    """A dlt source called `name` with the incremental append table `events` (ids 1..rows)."""

    @dlt.source(name=name)
    def _src() -> Any:
        yield events(rows)

    return _src()


def run_sync(root: Path, method: str, name: str, rows: int, **kwargs: Any) -> dict[str, Any]:
    """One real sync of events 1..rows as source `name` (config fields via `native=`)."""
    return sync(DltPipelineRunner(root), method, name, named_source(name, rows), **kwargs)


def failed_load(root: Path, method: str, name: str, rows: int) -> None:
    """A sync whose load fails after extract: the package and its matching state are kept."""
    with (
        patch.object(DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("lf")),
        pytest.raises(RuntimeError, match="lf"),
    ):
        run_sync(root, method, name, rows)


def ids(root: Path, name: str, table: str = "events") -> list[int] | str:
    """Sorted ids of `raw_<name>.<table>`, or the DuckDB error class when it cannot be read."""
    path = root / "data" / "warehouse.duckdb"
    if not path.exists():
        return "<no warehouse>"
    con = duckdb.connect(str(path), read_only=True)
    try:
        return sorted(
            r[0] for r in con.execute(f'SELECT id FROM "raw_{name}"."{table}"').fetchall()
        )
    except duckdb.Error as exc:
        return f"<{type(exc).__name__}>"
    finally:
        con.close()


def drop_schema(root: Path, schema: str) -> None:
    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    try:
        con.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        con.close()


def plant_orphan_state(root: Path, schema: str, pipeline_name: str) -> None:
    """An orphaned raw schema whose `_dlt_pipeline_state` table names `pipeline_name`."""
    con = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    try:
        con.execute(f'CREATE SCHEMA "{schema}"')
        con.execute(f'CREATE TABLE "{schema}"."t" AS SELECT 1 AS id')
        con.execute(
            f'CREATE TABLE "{schema}"."_dlt_pipeline_state" AS SELECT ? AS pipeline_name',
            [pipeline_name],
        )
    finally:
        con.close()


def db_clean(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, answer: str | None = None
) -> SimpleNamespace:
    """`dango db clean` in `root` through the real CLI: `--yes`, or answering the prompt with `answer`."""
    from dango.cli.main import cli

    monkeypatch.chdir(root)
    monkeypatch.setenv("COLUMNS", "300")
    args = ["db", "clean"] + (["--yes"] if answer is None else [])
    result = CliRunner().invoke(cli, args, input=answer, catch_exceptions=True)
    return SimpleNamespace(
        exit_code=result.exit_code, output=result.output, exception=result.exception
    )


def pipeline_dirs() -> list[str]:
    root = pipelines_dir()
    return sorted(p.name for p in root.iterdir()) if root.exists() else []
