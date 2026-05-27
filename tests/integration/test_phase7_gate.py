"""tests/integration/test_phase7_gate.py

Phase 7 gate — cross-module integration verification.

Tests the integration boundaries between Phase 7 modules: sync → profiling →
drift → PII → analysis in a single flow. Unlike TEST-030 which tested modules
in isolation, this file verifies the full feature set works together end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dango.analysis.config import save_monitors_config
from dango.analysis.drilldown import run_drill_down
from dango.analysis.models import ComparisonType, MonitorConfig, MonitorsConfig
from dango.exceptions import (
    AuthenticationError,
    DangoError,
    SessionExpiredError,
)
from dango.migrations.runner import MigrationRunner
from dango.utils.dango_db import _schema_initialized, connect
from dango.utils.post_sync import dispatch_post_sync_hooks
from dango.web.middleware.auth import AuthMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_schema_cache() -> None:
    """Clear the dango_db schema initialization cache for test isolation."""
    _schema_initialized.clear()


def _create_test_warehouse(tmp_path: Path) -> Path:
    """Create a DuckDB warehouse with test data for testshop.

    Returns:
        Path to the DuckDB file.
    """
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)

    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SCHEMA raw_testshop")
    conn.execute("""
        CREATE TABLE raw_testshop.orders (
            id INTEGER NOT NULL,
            total DOUBLE,
            status VARCHAR,
            email VARCHAR
        )
    """)
    conn.execute("""
        INSERT INTO raw_testshop.orders VALUES
        (1, 10.50, 'succeeded', 'alice@example.com'),
        (2, 25.00, 'succeeded', 'bob@example.com'),
        (3, 5.00, 'failed', 'charlie@example.com')
    """)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Class 1: Module imports
# ---------------------------------------------------------------------------


class TestModuleImports:
    """All Phase 7 modules are importable and key symbols are accessible."""

    def test_governance_importable(self) -> None:
        """dango.governance exports drift and PII functions."""
        from dango.governance import detect_drift_for_sources, scan_sources_for_pii

        assert callable(detect_drift_for_sources)
        assert callable(scan_sources_for_pii)

    def test_notebooks_importable(self) -> None:
        """dango.notebooks exports manager and locking functions."""
        from dango.notebooks import acquire_lock, release_lock, start_marimo

        assert callable(start_marimo)
        assert callable(acquire_lock)
        assert callable(release_lock)

    def test_analysis_importable(self) -> None:
        """dango.analysis exports metrics and formatter functions."""
        from dango.analysis import categorize_results, run_analysis

        assert callable(run_analysis)
        assert callable(categorize_results)


# ---------------------------------------------------------------------------
# Class 2: Route registration
# ---------------------------------------------------------------------------


class TestRouteRegistration:
    """Phase 7 routes are registered in the FastAPI app."""

    def test_phase7_routes_registered(self) -> None:
        """All Phase 7 API route paths are present in the app."""
        from dango.web.app import app

        paths = {getattr(route, "path", None) for route in app.routes}

        expected = [
            "/api/catalog/{source}/{table}/columns",
            "/api/governance/schema-drift",
            "/api/governance/pii",
            "/api/monitoring",
            "/api/notebooks",
        ]
        for path in expected:
            assert path in paths, f"Route {path} not registered in app"


# ---------------------------------------------------------------------------
# Class 3: CLI registration
# ---------------------------------------------------------------------------


class TestCLIRegistration:
    """Phase 7 CLI commands are registered."""

    def test_cli_commands_registered(self) -> None:
        """governance, notebook, monitor, and analyze commands exist."""
        from dango.cli.main import cli

        assert "governance" in cli.commands
        assert "notebook" in cli.commands
        assert "monitor" in cli.commands
        assert "analyze" in cli.commands


# ---------------------------------------------------------------------------
# Class 4: Full pipeline end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestFullPipelineEndToEnd:
    """Full sync → profiling → drift → PII → analysis chain."""

    def test_full_hook_chain_stores_all_data(self, tmp_path: Path) -> None:
        """dispatch_post_sync_hooks populates all four data stores."""
        _clear_schema_cache()
        _create_test_warehouse(tmp_path)

        # Set up monitors config so the analysis hook has work to do
        config = MonitorsConfig(
            enabled=True,
            monitors=[
                MonitorConfig(
                    name="gate_order_total",
                    source_table="raw_testshop.orders",
                    value_expression="SUM(total)",
                    filter="status = 'succeeded'",
                    compare=ComparisonType.week_over_week,
                    alert_threshold=20.0,
                ),
            ],
        )
        save_monitors_config(tmp_path, config)

        # Run the full hook chain — mock PII analyzer to avoid spaCy
        with patch("dango.governance.pii_detector._get_analyzer", return_value=None):
            dispatch_post_sync_hooks(tmp_path, ["testshop"])

        # Drift detection now runs pre-dbt (not from post_sync), call directly
        from dango.governance.schema_drift import detect_drift_for_sources

        detect_drift_for_sources(tmp_path, ["testshop"])

        with connect(tmp_path) as sqlite_conn:
            # 1. Profiling stats populated
            profiling = sqlite_conn.execute(
                "SELECT source FROM profiling_stats WHERE source = 'testshop'"
            ).fetchall()
            assert len(profiling) > 0, "profiling_stats should have rows for testshop"

            # 2. Schema baselines created (first run = baseline only)
            baselines = sqlite_conn.execute(
                "SELECT source FROM schema_baselines WHERE source = 'testshop'"
            ).fetchall()
            assert len(baselines) > 0, "schema_baselines should have baseline"

            # 3. Metric history populated
            metrics = sqlite_conn.execute(
                "SELECT metric_name, metric_value FROM metric_history "
                "WHERE metric_name = 'gate_order_total'"
            ).fetchall()
            assert len(metrics) >= 1, "metric_history should have metric value"
            assert metrics[0][1] == pytest.approx(35.5)  # 10.50 + 25.00

            # 4. No drift events on first run (baseline only)
            drift = sqlite_conn.execute(
                "SELECT source FROM drift_events WHERE source = 'testshop'"
            ).fetchall()
            assert len(drift) == 0, "First run should not produce drift events"


# ---------------------------------------------------------------------------
# Class 5: Analysis with drill-down
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestAnalysisWithDrillDown:
    """Analysis metric execution + drill-down end-to-end."""

    def test_metric_execution_and_drilldown(self, tmp_path: Path) -> None:
        """run_analysis stores results; run_drill_down returns contributors."""
        _clear_schema_cache()
        db_path = _create_test_warehouse(tmp_path)

        # Set up monitors with a drill-down dimension
        metric = MonitorConfig(
            name="gate_drilldown_total",
            source_table="raw_testshop.orders",
            value_expression="SUM(total)",
            compare=ComparisonType.week_over_week,
            alert_threshold=20.0,
            drill_down=["status"],
        )
        config = MonitorsConfig(enabled=True, monitors=[metric])
        save_monitors_config(tmp_path, config)

        # Run analysis via public API
        from dango.analysis import run_analysis

        results = run_analysis(tmp_path)
        assert len(results) >= 1
        assert results[0].metric.metric_name == "gate_drilldown_total"

        # Run drill-down directly — first call creates baseline snapshot
        dimensions = run_drill_down(db_path, tmp_path, metric)
        assert len(dimensions) >= 1
        assert dimensions[0].dimension == "status"
        # First run has no previous snapshot → no contributors (expected)

        # Second call compares against the baseline → contributors populated
        dimensions2 = run_drill_down(db_path, tmp_path, metric)
        assert len(dimensions2) >= 1
        assert dimensions2[0].dimension == "status"
        assert len(dimensions2[0].contributors) > 0


# ---------------------------------------------------------------------------
# Class 6: RBAC enforcement
# ---------------------------------------------------------------------------

# DangoError → HTTP status mapping (auth-subset of app.py)
_STATUS_MAP: dict[type[DangoError], int] = {
    SessionExpiredError: 401,
    AuthenticationError: 401,
    DangoError: 500,
}


@pytest.mark.integration
class TestRBACEnforcement:
    """Phase 7 endpoints reject unauthenticated requests."""

    @pytest.fixture
    def phase7_client(self, tmp_path: Path) -> TestClient:
        """FastAPI test client with auth middleware + Phase 7 routers."""
        from dango.web.routes.catalog import router as catalog_router
        from dango.web.routes.governance import router as governance_router
        from dango.web.routes.monitoring import router as monitoring_router
        from dango.web.routes.notebooks import router as notebooks_router

        # Set up auth database
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        db_path = dango_dir / "auth.db"
        migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
        runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
        runner.apply_pending()
        (dango_dir / "auth.yml").write_text("enabled: true\n")

        app = FastAPI()
        app.state.project_root = tmp_path
        app.add_middleware(AuthMiddleware, project_root=tmp_path, idle_timeout_minutes=60)
        app.include_router(catalog_router)
        app.include_router(governance_router)
        app.include_router(monitoring_router)
        app.include_router(notebooks_router)

        @app.exception_handler(DangoError)
        async def _handler(request: Request, exc: DangoError) -> JSONResponse:
            status_code = 500
            for cls in type(exc).__mro__:
                if cls in _STATUS_MAP:
                    status_code = _STATUS_MAP[cls]
                    break
            return JSONResponse(
                status_code=status_code,
                content={"error_code": exc.error_code, "message": exc.user_message},
            )

        return TestClient(app, raise_server_exceptions=False)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/governance/schema-drift",
            "/api/monitoring",
            "/api/catalog/test/test/columns",
            "/api/notebooks",
        ],
    )
    def test_phase7_endpoints_require_auth(self, phase7_client: TestClient, path: str) -> None:
        """Unauthenticated GET to Phase 7 endpoints returns 401."""
        resp = phase7_client.get(path)
        assert resp.status_code == 401, f"{path} returned {resp.status_code}, expected 401"
