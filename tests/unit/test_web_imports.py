"""tests/unit/test_web_imports.py

Import smoke tests for the web module after TASK-085 refactoring.
Verifies all route modules import cleanly and the app creates successfully.
"""

import pytest


@pytest.mark.unit
class TestWebImports:
    """Verify that all web modules are importable after the TASK-085 split."""

    def test_import_app(self):
        """App module imports and creates FastAPI instance."""
        from dango.web.app import app

        assert app is not None
        assert app.title == "Dango API"

    def test_import_models(self):
        """All Pydantic models are importable."""
        from dango.web.models import (
            LogEntry,
            ServiceHealth,
            SourceStatus,
            SyncRequest,
            SyncResponse,
            TableInfo,
            WatcherStatus,
        )

        assert TableInfo is not None
        assert SourceStatus is not None
        assert ServiceHealth is not None
        assert SyncRequest is not None
        assert SyncResponse is not None
        assert LogEntry is not None
        assert WatcherStatus is not None

    def test_import_helpers(self):
        """Helper functions are importable."""
        from dango.web.helpers import (
            append_log_entry,
            check_service_status_async,
            get_dbt_models,
            get_duckdb_path,
            get_project_root,
            get_source_freshness,
            load_all_logs,
            load_sources_config,
            mask_sensitive_config,
        )

        assert get_project_root is not None
        assert load_sources_config is not None
        assert get_duckdb_path is not None
        assert get_dbt_models is not None
        assert mask_sensitive_config is not None
        assert get_source_freshness is not None
        assert append_log_entry is not None
        assert load_all_logs is not None
        assert check_service_status_async is not None

    def test_import_websocket_route(self):
        """WebSocket route module imports with ws_manager."""
        from dango.web.routes.websocket import ConnectionManager, router, ws_manager

        assert router is not None
        assert ws_manager is not None
        assert isinstance(ws_manager, ConnectionManager)

    def test_import_health_route(self):
        """Health route module imports."""
        from dango.web.routes.health import router

        assert router is not None

    def test_import_config_route(self):
        """Config route module imports."""
        from dango.web.routes.config import router

        assert router is not None

    def test_import_logs_route(self):
        """Logs route module imports."""
        from dango.web.routes.logs import router

        assert router is not None

    def test_import_ui_route(self):
        """UI route module imports."""
        from dango.web.routes.ui import router

        assert router is not None

    def test_import_sources_route(self):
        """Sources route module imports."""
        from dango.web.routes.sources import router

        assert router is not None

    def test_import_sync_route(self):
        """Sync route module imports."""
        from dango.web.routes.sync import router

        assert router is not None

    def test_import_dbt_route(self):
        """dbt route module imports."""
        from dango.web.routes.dbt import router

        assert router is not None

    def test_import_upload_route(self):
        """Upload route module imports."""
        from dango.web.routes.upload import router

        assert router is not None

    def test_import_metabase_proxy_route(self):
        """Metabase proxy route module imports."""
        from dango.web.routes.metabase_proxy import router

        assert router is not None

    def test_app_has_routes_registered(self):
        """App instance has all routers included (routes are registered)."""
        from dango.web.app import app

        # Collect all route paths
        route_paths = [route.path for route in app.routes]

        # Verify key endpoints exist
        expected_paths = [
            "/api/status",
            "/api/config",
            "/api/sources",
            "/api/logs",
            "/api/dbt/models",
            "/ws",
            "/",
            "/health",
            "/logs",
        ]

        for path in expected_paths:
            assert path in route_paths, f"Route {path} not found in app routes"

    def test_web_init_import(self):
        """web/__init__.py import still works."""
        from dango.web import app as web_app_module

        assert web_app_module is not None
