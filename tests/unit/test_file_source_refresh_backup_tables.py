"""tests/unit/test_file_source_refresh_backup_tables.py

WP-64: a leftover <name>_pre_refresh_backup table is never treated as a source table (real DuckDB).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dango.analysis.templates import generate_metrics_for_source
from dango.catalog.schema import _get_raw_tables_from_duckdb
from dango.config.models import (
    CSVSourceConfig,
    DataSource,
    LocalFilesSourceConfig,
    SourceType,
)
from dango.ingestion.dlt_runner import DltPipelineRunner
from dango.transformation.generator import DbtModelGenerator

NAME = "orders"
SCHEMA = f"raw_{NAME}"
BACKUP = f"{NAME}_pre_refresh_backup"


@pytest.fixture(params=["csv", "local_files"])
def kind(request) -> str:
    return request.param


@pytest.fixture(params=["backup_only", "backup_and_partial_live"])
def orphan_state(request) -> str:
    """The two states a hard kill in the middle of a full refresh leaves behind."""
    return request.param


def _source(kind: str, tmp_path: Path) -> DataSource:
    if kind == "csv":
        cfg = CSVSourceConfig(directory=tmp_path / "uploads", file_pattern="*.csv")
        return DataSource(name=NAME, type=SourceType.CSV, csv=cfg)
    cfg = LocalFilesSourceConfig(directory=tmp_path / "uploads", file_pattern="*.csv")
    return DataSource(name=NAME, type=SourceType.LOCAL_FILES, local_files=cfg)


def _sync(runner: DltPipelineRunner, src: DataSource, **kw):
    fn = runner._run_csv_source if src.type == SourceType.CSV else runner._run_local_files_source
    return fn(src, **kw)


def _tables(db: Path) -> set[str]:
    conn = duckdb.connect(str(db), config={"access_mode": "read_only"})
    try:
        rows = conn.execute(
            "SELECT table_schema, table_name FROM information_schema.tables"
        ).fetchall()
    finally:
        conn.close()
    return {f"{s}.{t}" for s, t in rows}


@pytest.fixture
def orphaned(kind, orphan_state, tmp_path):
    """A synced project whose raw schema holds a backup table from a killed refresh."""
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.csv").write_text("id,item\n1,a\n2,b\n", encoding="utf-8")
    runner = DltPipelineRunner(tmp_path)
    src = _source(kind, tmp_path)
    assert _sync(runner, src)["status"] == "success"
    conn = duckdb.connect(str(runner.duckdb_path))
    try:
        if orphan_state == "backup_only":
            conn.execute(f'ALTER TABLE "{SCHEMA}"."{NAME}" RENAME TO "{BACKUP}"')
        else:
            conn.execute(f'CREATE TABLE "{SCHEMA}"."{BACKUP}" AS SELECT * FROM "{SCHEMA}"."{NAME}"')
    finally:
        conn.close()
    return tmp_path, runner, src


def test_generator_makes_no_staging_model_for_the_backup(orphaned):
    tmp_path, _runner, src = orphaned

    DbtModelGenerator(tmp_path).generate_all_models([src])

    staging = tmp_path / "dbt" / "models" / "staging"
    names = sorted(p.name for p in staging.glob("*")) if staging.exists() else []
    assert not [n for n in names if "pre_refresh_backup" in n]
    for path in staging.glob("*") if staging.exists() else []:
        assert "pre_refresh_backup" not in path.read_text(encoding="utf-8")


def test_generator_models_only_the_live_table(orphaned, orphan_state):
    tmp_path, _runner, src = orphaned

    DbtModelGenerator(tmp_path).generate_all_models([src])

    staging = tmp_path / "dbt" / "models" / "staging"
    models = sorted(p.name for p in staging.glob("stg_*.sql")) if staging.exists() else []
    expected = [] if orphan_state == "backup_only" else [f"stg_{NAME}__{NAME}.sql"]
    assert models == expected


def test_monitor_templates_leave_out_the_backup(orphaned, kind):
    tmp_path, _runner, _src = orphaned

    metrics = generate_metrics_for_source(kind, NAME, project_root=tmp_path)

    assert not [m for m in metrics if "pre_refresh_backup" in m.source_table]
    assert not [m for m in metrics if "pre_refresh_backup" in m.name]


def test_catalog_raw_table_discovery_leaves_out_the_backup(orphaned):
    _tmp_path, runner, _src = orphaned

    found = _get_raw_tables_from_duckdb(runner.duckdb_path)

    assert not [t for t in found if "pre_refresh_backup" in t["table"]]


def test_next_sync_works_and_keeps_the_orphan_untouched(orphaned):
    tmp_path, runner, src = orphaned
    (tmp_path / "uploads" / "b.csv").write_text("id,item\n3,c\n", encoding="utf-8")

    result = _sync(runner, src)

    assert result["status"] == "success"
    assert f"{SCHEMA}.{BACKUP}" in _tables(runner.duckdb_path)  # never deleted automatically
    DbtModelGenerator(tmp_path).generate_all_models([src])
    staging = tmp_path / "dbt" / "models" / "staging"
    assert not list(staging.glob("*pre_refresh_backup*"))


def test_backup_name_helpers():
    from dango.utils.refresh_backup import is_refresh_backup_table, refresh_backup_table_name

    assert refresh_backup_table_name("orders") == "orders_pre_refresh_backup"
    assert is_refresh_backup_table("orders_pre_refresh_backup")
    assert not is_refresh_backup_table("orders")
    assert not is_refresh_backup_table("pre_refresh_backup_orders")
