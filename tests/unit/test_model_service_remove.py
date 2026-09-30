"""tests/unit/test_model_service_remove.py

Tests for remove_model: downstream detection, DbtLock-guarded table drop, docs and monitors cleanup.
"""

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
import yaml

from dango.exceptions import DbtLockError
from dango.transformation.model_service import ModelServiceError, remove_model

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, content: str = "select 1") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    _write(tmp_path, "dbt/models/staging/stg_orders__orders.sql", "select 1 as id")
    return tmp_path


# ---- remove ----------------------------------------------------------------


def _warehouse(proj: Path, layer: str, table: str) -> Path:
    db = proj / "data" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db))
    conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{layer}"')
    conn.execute(f'CREATE TABLE "{layer}"."{table}" AS SELECT 1 AS id')
    conn.close()
    return db


def _tables(db: Path) -> list[str]:
    conn = duckdb.connect(str(db), config={"access_mode": "read_only"})
    try:
        return [
            r[0]
            for r in conn.execute("select table_name from information_schema.tables").fetchall()
        ]
    finally:
        conn.close()


def test_remove_model_dry_run_reports_downstream_and_table(proj: Path) -> None:
    _write(proj, "dbt/models/intermediate/int_a.sql", "select 1")
    _write(proj, "dbt/models/marts/fct_b.sql", "select * from {{ ref( 'int_a' ) }}")
    _write(proj, "dbt/models/marts/fct_c.sql", "select * from {{ ref('pkg', \"int_a\") }}")
    db = _warehouse(proj, "intermediate", "int_a")
    res = remove_model(proj, "int_a", dry_run=True)
    assert res.status == "dry_run"
    assert sorted(res.downstream) == ["marts.fct_b", "marts.fct_c"]
    assert res.table_existed is True
    assert (proj / "dbt/models/intermediate/int_a.sql").exists()
    assert "int_a" in _tables(db)


def test_remove_model_blocks_on_downstream_without_force(proj: Path) -> None:
    _write(proj, "dbt/models/intermediate/int_a.sql", "select 1")
    _write(proj, "dbt/models/marts/fct_b.sql", "select * from {{ ref('int_a') }}")
    with pytest.raises(ModelServiceError):
        remove_model(proj, "int_a")
    assert (proj / "dbt/models/intermediate/int_a.sql").exists()
    assert remove_model(proj, "int_a", force=True).status == "removed"


def test_remove_model_rejects_staging_and_unknown(proj: Path) -> None:
    with pytest.raises(ModelServiceError):
        remove_model(proj, "stg_orders__orders")
    with pytest.raises(ModelServiceError):
        remove_model(proj, "nope")


def test_remove_model_drops_table_under_lock(proj: Path) -> None:
    from dango.utils import DbtLock

    _write(proj, "dbt/models/marts/finance/fct_a.sql", "select 1")
    db = _warehouse(proj, "marts", "fct_a")
    events: list[str] = []
    real_acquire = DbtLock.acquire
    real_connect = duckdb.connect

    def acquire(self, timeout=300):  # type: ignore[no-untyped-def]
        events.append(f"acquire:{timeout}")
        return real_acquire(self, timeout)

    def connect(*a, **k):  # type: ignore[no-untyped-def]
        if "config" not in k:
            events.append("rw-connect")
            assert any(e.startswith("acquire") for e in events)
        return real_connect(*a, **k)

    with patch.object(DbtLock, "acquire", acquire), patch("duckdb.connect", connect):
        res = remove_model(proj, "fct_a", lock_timeout=7)
    assert res.dropped_table is True and events == ["acquire:7", "rw-connect"]
    assert "fct_a" not in _tables(db)
    assert not (proj / "dbt/models/marts/finance/fct_a.sql").exists()


def test_remove_model_lock_held_nothing_deleted(proj: Path) -> None:
    from dango.utils import DbtLock

    sql = _write(proj, "dbt/models/marts/fct_a.sql", "select 1")
    db = _warehouse(proj, "marts", "fct_a")
    with patch.object(DbtLock, "acquire", side_effect=DbtLockError("busy")):
        with pytest.raises(ModelServiceError):
            remove_model(proj, "fct_a")
    assert sql.exists() and "fct_a" in _tables(db)


def test_remove_model_no_drop_needs_no_lock(proj: Path) -> None:
    from dango.utils import DbtLock

    _write(proj, "dbt/models/marts/fct_a.sql", "select 1")
    db = _warehouse(proj, "marts", "fct_a")
    with patch.object(DbtLock, "acquire", side_effect=AssertionError("no lock expected")):
        res = remove_model(proj, "fct_a", drop_table=False)
    assert res.dropped_table is False and "fct_a" in _tables(db)


def test_remove_model_docs_keeps_file_with_other_keys(proj: Path) -> None:
    _write(proj, "dbt/models/marts/fct_a.sql", "select 1")
    _write(proj, "dbt/models/marts/fct_b.sql", "select 1")
    a = _write(
        proj,
        "dbt/models/marts/schema.yml",
        yaml.dump({"version": 2, "exposures": [{"name": "e"}], "models": [{"name": "fct_a"}]}),
    )
    remove_model(proj, "fct_a")
    assert yaml.safe_load(a.read_text()) == {
        "version": 2,
        "exposures": [{"name": "e"}],
        "models": [],
    }
    b = _write(
        proj, "dbt/models/marts/other.yml", yaml.dump({"version": 2, "models": [{"name": "fct_b"}]})
    )
    remove_model(proj, "fct_b")
    assert not b.exists()
