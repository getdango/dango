"""tests/unit/test_monitoring_dbt_tests.py

Tests for dbt test results in the monitoring endpoint.

Covers:
- _read_dbt_test_results() parsing manifest + run_results
- MonitoringResponse includes dbt_tests array
- Empty/missing files return empty array
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dango.web.routes.monitoring import (
    DbtTestResult,
    _build_monitoring_response,
    _read_dbt_test_results,
)


def _write_json(path: Path, data: dict) -> None:
    """Write a JSON file, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.mark.unit
class TestReadDbtTestResults:
    """_read_dbt_test_results parses manifest + run_results."""

    def test_returns_empty_when_no_manifest(self, tmp_path):
        """No manifest.json → empty list."""
        assert _read_dbt_test_results(tmp_path) == []

    def test_returns_empty_when_no_test_nodes(self, tmp_path):
        """Manifest with no test nodes → empty list."""
        manifest = {"nodes": {"model.dango.stg_shopify__orders": {"resource_type": "model"}}}
        _write_json(tmp_path / "dbt" / "target" / "manifest.json", manifest)
        assert _read_dbt_test_results(tmp_path) == []

    def test_parses_test_with_results(self, tmp_path):
        """Test nodes matched with run_results get status + timing."""
        manifest = {
            "nodes": {
                "test.dango.not_null_stg_shopify__orders_order_id": {
                    "resource_type": "test",
                    "name": "not_null_stg_shopify__orders_order_id",
                    "depends_on": {"nodes": ["model.dango.stg_shopify__orders"]},
                },
                "test.dango.unique_stg_shopify__orders_order_id": {
                    "resource_type": "test",
                    "name": "unique_stg_shopify__orders_order_id",
                    "depends_on": {"nodes": ["model.dango.stg_shopify__orders"]},
                },
            }
        }
        run_results = {
            "results": [
                {
                    "unique_id": "test.dango.not_null_stg_shopify__orders_order_id",
                    "status": "pass",
                    "execution_time": 0.12,
                },
                {
                    "unique_id": "test.dango.unique_stg_shopify__orders_order_id",
                    "status": "fail",
                    "execution_time": 0.08,
                },
            ]
        }
        _write_json(tmp_path / "dbt" / "target" / "manifest.json", manifest)
        _write_json(tmp_path / "dbt" / "target" / "run_results.json", run_results)

        results = _read_dbt_test_results(tmp_path)
        assert len(results) == 2

        by_name = {r.test_name: r for r in results}

        not_null = by_name["not_null_stg_shopify__orders_order_id"]
        assert not_null.status == "pass"
        assert not_null.model_name == "stg_shopify__orders"
        assert not_null.execution_time == 0.12

        unique = by_name["unique_stg_shopify__orders_order_id"]
        assert unique.status == "fail"
        assert unique.execution_time == 0.08

    def test_no_run_results_gives_none_status(self, tmp_path):
        """Test nodes without run_results get status=None."""
        manifest = {
            "nodes": {
                "test.dango.not_null_stg_csv__data_id": {
                    "resource_type": "test",
                    "name": "not_null_stg_csv__data_id",
                    "depends_on": {"nodes": ["model.dango.stg_csv__data"]},
                },
            }
        }
        _write_json(tmp_path / "dbt" / "target" / "manifest.json", manifest)

        results = _read_dbt_test_results(tmp_path)
        assert len(results) == 1
        assert results[0].status is None
        assert results[0].model_name == "stg_csv__data"

    def test_handles_error_status(self, tmp_path):
        """Error status from run_results is preserved."""
        manifest = {
            "nodes": {
                "test.dango.unique_stg_csv__data_id": {
                    "resource_type": "test",
                    "name": "unique_stg_csv__data_id",
                    "depends_on": {"nodes": ["model.dango.stg_csv__data"]},
                },
            }
        }
        run_results = {
            "results": [
                {
                    "unique_id": "test.dango.unique_stg_csv__data_id",
                    "status": "error",
                    "execution_time": 0.0,
                }
            ]
        }
        _write_json(tmp_path / "dbt" / "target" / "manifest.json", manifest)
        _write_json(tmp_path / "dbt" / "target" / "run_results.json", run_results)

        results = _read_dbt_test_results(tmp_path)
        assert results[0].status == "error"

    def test_skips_non_test_nodes(self, tmp_path):
        """Model and source nodes should be excluded."""
        manifest = {
            "nodes": {
                "model.dango.stg_shopify__orders": {
                    "resource_type": "model",
                    "name": "stg_shopify__orders",
                },
                "test.dango.not_null_orders_id": {
                    "resource_type": "test",
                    "name": "not_null_orders_id",
                    "depends_on": {"nodes": ["model.dango.stg_shopify__orders"]},
                },
            }
        }
        _write_json(tmp_path / "dbt" / "target" / "manifest.json", manifest)

        results = _read_dbt_test_results(tmp_path)
        assert len(results) == 1
        assert results[0].test_name == "not_null_orders_id"


@pytest.mark.unit
class TestBuildMonitoringResponseWithDbtTests:
    """_build_monitoring_response includes dbt_tests."""

    def test_includes_dbt_tests(self):
        """Response should include dbt_tests when provided."""
        dbt_tests = [
            DbtTestResult(test_name="not_null_id", status="pass", model_name="stg_csv__data"),
        ]
        response = _build_monitoring_response([], dbt_tests=dbt_tests)
        assert len(response.dbt_tests) == 1
        assert response.dbt_tests[0].test_name == "not_null_id"

    def test_empty_dbt_tests_default(self):
        """Response should have empty dbt_tests by default."""
        response = _build_monitoring_response([])
        assert response.dbt_tests == []

    def test_response_model_serialization(self):
        """MonitoringResponse should serialize dbt_tests correctly."""
        dbt_tests = [
            DbtTestResult(
                test_name="unique_id",
                status="fail",
                model_name="stg_shopify__orders",
                execution_time=0.05,
            ),
        ]
        response = _build_monitoring_response([], dbt_tests=dbt_tests)
        data = response.model_dump()
        assert "dbt_tests" in data
        assert data["dbt_tests"][0]["test_name"] == "unique_id"
        assert data["dbt_tests"][0]["status"] == "fail"
        assert data["dbt_tests"][0]["execution_time"] == 0.05
