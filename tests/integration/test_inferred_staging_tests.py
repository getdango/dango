"""tests/integration/test_inferred_staging_tests.py

Staging data tests inferred from a data sample must not turn a later `dbt build` red when
later data differs (real dbt + DuckDB); key checks stay blocking.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from dango.config.models import DataSource, SourceType
from dango.transformation.generator import DbtModelGenerator
from dango.utils.post_sync import _enrich_staging_tests, _run_profiling

pytestmark = pytest.mark.integration

SOURCE = "shop"
MODEL = "stg_shop__orders"
WARN_NOT_NULL = {"not_null": {"config": {"severity": "warn"}}}


def _warehouse(root: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(root / "data" / "warehouse.duckdb"))


def _make_project(tmp_path: Path, *, declared_not_null: bool = False) -> Path:
    """A project with a loaded raw table (no blanks), a dbt project and a downstream model."""
    root = tmp_path / "proj"
    (root / "data").mkdir(parents=True)
    conn = _warehouse(root)
    conn.execute("CREATE SCHEMA raw_shop")
    status_ddl = "VARCHAR NOT NULL" if declared_not_null else "VARCHAR"
    conn.execute(
        f"CREATE TABLE raw_shop.orders (id INTEGER, customer_id INTEGER, status {status_ddl})"
    )
    conn.execute(
        "INSERT INTO raw_shop.orders VALUES (1, 10, 'paid'), (2, 11, 'paid'), (3, 12, 'x')"
    )
    conn.close()

    dbt_dir = root / "dbt"
    (dbt_dir / "models" / "staging").mkdir(parents=True)
    (dbt_dir / "models" / "marts").mkdir(parents=True)
    (dbt_dir / "dbt_project.yml").write_text(
        "name: t\nversion: '1.0.0'\nprofile: t\nmodel-paths: [models]\n",
        encoding="utf-8",
        newline="\n",
    )
    (dbt_dir / "profiles.yml").write_text(
        "t:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {(root / 'data' / 'warehouse.duckdb').as_posix()}\n",
        encoding="utf-8",
        newline="\n",
    )
    (dbt_dir / "models" / "marts" / "order_count.sql").write_text(
        "select count(*) as n from {{ ref('stg_shop__orders') }}\n",
        encoding="utf-8",
        newline="\n",
    )
    return root


def _sync(root: Path) -> None:
    """What a sync does around dbt: generate staging files, profile, enrich tests."""
    source = DataSource(name=SOURCE, type=SourceType.STRIPE)
    summary = DbtModelGenerator(root).generate_all_models([source])
    assert summary["errors"] == []
    _run_profiling(root, [SOURCE])
    _enrich_staging_tests(root, [SOURCE])


def _schema_file(root: Path) -> Path:
    return root / "dbt" / "models" / "staging" / "stg_shop.yml"


def _columns(root: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(_schema_file(root).read_text(encoding="utf-8"))
    return {c["name"]: c for c in data["models"][0]["columns"]}


def _dbt_build(root: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = dict(os.environ)
    env.update(
        HOME=str(tmp_path / "home"),
        DBT_SEND_ANONYMOUS_USAGE_STATS="False",
        DO_NOT_TRACK="1",
    )
    dbt_dir = root / "dbt"
    result = subprocess.run(
        [
            str(Path(sys.executable).parent / "dbt"),
            "build",
            "--project-dir",
            str(dbt_dir),
            "--profiles-dir",
            str(dbt_dir),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )
    run_results = json.loads((dbt_dir / "target" / "run_results.json").read_text(encoding="utf-8"))
    statuses = {r["unique_id"].split(".")[2]: r["status"] for r in run_results["results"]}
    return result, statuses


def _insert(root: Path, values_sql: str) -> None:
    conn = _warehouse(root)
    conn.execute(f"INSERT INTO raw_shop.orders VALUES {values_sql}")
    conn.close()


def test_baseline_first_sync_builds_green(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    result, statuses = _dbt_build(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert statuses["order_count"] == "success"


def test_blank_in_ordinary_column_later_warns_instead_of_failing(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    _insert(root, "(4, NULL, 'paid')")  # a later sync with a blank cell
    _sync(root)
    result, statuses = _dbt_build(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert statuses[f"not_null_{MODEL}_customer_id"] == "warn"
    assert statuses["order_count"] == "success"  # downstream model still built


def test_ordinary_column_inferred_test_is_warn_severity(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    cols = _columns(root)
    assert cols["customer_id"]["tests"] == [WARN_NOT_NULL]
    assert cols["status"]["tests"] == [WARN_NOT_NULL]


def test_inferred_test_kinds_are_limited_to_not_null_and_unique(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    kinds: set[str] = set()
    for col in _columns(root).values():
        for test in col.get("tests", []):
            kinds.add(test if isinstance(test, str) else next(iter(test)))
    assert kinds <= {"not_null", "unique"}


def test_key_column_keeps_blocking_not_null_and_unique(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    assert _columns(root)["id"]["tests"] == ["unique", "not_null"]


def test_blank_key_still_fails_build(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    _insert(root, "(NULL, 13, 'paid')")
    _sync(root)
    result, statuses = _dbt_build(root, tmp_path)
    assert result.returncode != 0
    assert statuses[f"not_null_{MODEL}_id"] == "fail"


def test_duplicate_key_still_fails_build(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    _insert(root, "(1, 13, 'paid')")
    _sync(root)
    result, statuses = _dbt_build(root, tmp_path)
    assert result.returncode != 0
    # The raw-table uniqueness check fails first; the dependent staging check never runs.
    assert statuses["source_unique_shop_orders_id"] == "fail"


def test_declared_not_null_column_stays_blocking(tmp_path: Path) -> None:
    root = _make_project(tmp_path, declared_not_null=True)
    _sync(root)
    assert _columns(root)["status"]["tests"] == ["not_null"]


def test_generation_after_profiling_writes_warn_severity(tmp_path: Path) -> None:
    """Profiling data already present when the YAML is generated takes the same route."""
    root = _make_project(tmp_path)
    _run_profiling(root, [SOURCE])
    DbtModelGenerator(root).generate_all_models([DataSource(name=SOURCE, type=SourceType.STRIPE)])
    cols = _columns(root)
    assert cols["customer_id"]["tests"] == [WARN_NOT_NULL]
    assert cols["id"]["tests"] == ["unique", "not_null"]
    result, _ = _dbt_build(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_customized_model_yaml_is_not_rewritten(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    source = DataSource(name=SOURCE, type=SourceType.STRIPE)
    DbtModelGenerator(root).generate_all_models([source])
    staging = root / "dbt" / "models" / "staging"
    sql = staging / f"{MODEL}.sql"
    sql.write_text(
        sql.read_text(encoding="utf-8").replace("-- Auto-generated by Dango", "-- Mine"),
        encoding="utf-8",
        newline="\n",
    )
    schema = _schema_file(root)
    schema.write_text(
        schema.read_text(encoding="utf-8") + "\n# my notes\n", encoding="utf-8", newline="\n"
    )
    before = schema.read_bytes()
    _run_profiling(root, [SOURCE])
    _enrich_staging_tests(root, [SOURCE])
    assert schema.read_bytes() == before


def test_repeated_sync_does_not_change_the_schema_file(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    _sync(root)
    first = _schema_file(root).read_bytes()
    _sync(root)
    assert _schema_file(root).read_bytes() == first
