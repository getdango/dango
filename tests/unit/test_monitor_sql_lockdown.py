"""tests/unit/test_monitor_sql_lockdown.py

Monitor and drill-down SQL runs on the locked read-only query connection, using a real DuckDB file.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from dango.analysis.drilldown import _query_dimension_breakdown
from dango.analysis.metrics import _execute_metric
from dango.analysis.models import MonitorConfig
from dango.analysis.templates import _stripe_metrics

pytestmark = pytest.mark.unit


@pytest.fixture
def warehouse(tmp_path: Path) -> Path:
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE SCHEMA raw_stripe")
    conn.execute(
        "CREATE TABLE raw_stripe.charge (amount BIGINT, status VARCHAR, refunded BOOLEAN, "
        "currency VARCHAR)"
    )
    conn.execute(
        "INSERT INTO raw_stripe.charge VALUES (1000, 'succeeded', false, 'usd'), "
        "(3000, 'succeeded', true, 'eur'), (500, 'failed', false, 'usd')"
    )
    conn.execute("CREATE SCHEMA raw_stripe_x")
    conn.execute("CREATE TABLE raw_stripe.customer (id VARCHAR)")
    conn.execute("INSERT INTO raw_stripe.customer VALUES ('a'), ('b')")
    conn.execute('CREATE VIEW raw_stripe_x.v AS SELECT * FROM "warehouse"."raw_stripe"."customer"')
    conn.close()
    return path


@pytest.fixture
def canary(tmp_path: Path) -> Path:
    path = tmp_path / "canary.txt"
    path.write_text("secret-canary-content")
    return path


def _monitor(**kwargs: object) -> MonitorConfig:
    defaults: dict[str, object] = {
        "name": "m",
        "source_table": "raw_stripe.charge",
        "value_expression": "COUNT(*)",
    }
    defaults.update(kwargs)
    return MonitorConfig(**defaults)  # type: ignore[arg-type]


def test_metric_still_evaluates(warehouse: Path) -> None:
    result = _execute_metric(warehouse, _monitor(value_expression="SUM(amount)"))
    assert result.error is None
    assert result.value == 4500.0


def test_metric_expression_cannot_read_files(warehouse: Path, canary: Path) -> None:
    metric = _monitor(value_expression=f"(SELECT length(content) FROM read_text('{canary}'))")
    result = _execute_metric(warehouse, metric)
    assert result.error is not None
    assert result.value is None


def test_metric_filter_cannot_read_files(warehouse: Path, canary: Path) -> None:
    metric = _monitor(filter=f"amount < (SELECT length(content) FROM read_text('{canary}'))")
    result = _execute_metric(warehouse, metric)
    assert result.error is not None
    assert result.value is None


def test_metric_source_table_cannot_read_files(warehouse: Path, canary: Path) -> None:
    metric = _monitor(source_table=f"read_csv('{canary}')", value_expression="COUNT(*)")
    result = _execute_metric(warehouse, metric)
    assert result.error is not None
    assert result.value is None


def test_metric_cannot_write_files(warehouse: Path, tmp_path: Path) -> None:
    # COPY is a statement, not an expression; the only write route from a SELECT is
    # a table function in FROM, which the locked connection rejects.
    target = tmp_path / "out.csv"
    metric = _monitor(source_table=f"(COPY raw_stripe.charge TO '{target}') t")
    result = _execute_metric(warehouse, metric)
    assert result.error is not None
    assert not target.exists()


def test_drilldown_value_expression_cannot_read_files(warehouse: Path, canary: Path) -> None:
    metric = _monitor(value_expression=f"MAX(length((SELECT content FROM read_text('{canary}'))))")
    assert _query_dimension_breakdown(warehouse, metric, "currency") == {}


def test_drilldown_dimension_cannot_read_files(warehouse: Path, canary: Path) -> None:
    dimension = f"(SELECT content FROM read_text('{canary}'))"
    assert _query_dimension_breakdown(warehouse, _monitor(), dimension) == {}


def test_drilldown_still_evaluates(warehouse: Path) -> None:
    metric = _monitor(value_expression="SUM(amount)")
    assert _query_dimension_breakdown(warehouse, metric, "currency") == {
        "eur": 3000.0,
        "usd": 1500.0,
    }


def test_stripe_templates_evaluate(warehouse: Path) -> None:
    for metric in _stripe_metrics("stripe"):
        result = _execute_metric(warehouse, metric)
        assert result.error is None, metric.name
        assert result.value is not None, metric.name


def test_view_with_catalog_qualified_body_resolves(warehouse: Path) -> None:
    metric = _monitor(source_table="raw_stripe_x.v")
    result = _execute_metric(warehouse, metric)
    assert result.error is None
    assert result.value == 2.0
