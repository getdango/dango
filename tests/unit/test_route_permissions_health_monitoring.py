"""tests/unit/test_route_permissions_health_monitoring.py

Role matrix for the health routes, monitoring run and table-profile refresh, with auth enabled and disabled.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from dango.auth.models import Role
from dango.web.routes.catalog import router as catalog_router
from dango.web.routes.health import router as health_router
from dango.web.routes.monitoring import router as monitoring_router
from tests.factories.auth_app_factories import AuthProject, build_client

_CSRF = {"X-Requested-With": "XMLHttpRequest"}

# (method, path)
HEALTH_ROUTES = [
    ("GET", "/api/status"),
    ("GET", "/api/watcher/status"),
    ("GET", "/api/health/platform"),
]
MONITORING_RUN = ("POST", "/api/monitoring/run")
TABLE_PROFILE = ("POST", "/api/catalog/events/orders/profile")
ALL_ROUTES = [*HEALTH_ROUTES, MONITORING_RUN, TABLE_PROFILE]

# Role -> expected status on each route group (health read vs. state-changing).
HEALTH_EXPECTED = {Role.VIEWER: 200, Role.EDITOR: 200, Role.ADMIN: 200}
MUTATING_EXPECTED = {Role.VIEWER: 403, Role.EDITOR: 200, Role.ADMIN: 200}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    return p


@pytest.fixture(autouse=True)
def stub_handlers(tmp_path: Path) -> Iterator[None]:
    """Replace handler internals so a permitted request answers 200 without real services."""
    platform_data = {
        "db_health": {"status": "ok"},
        "disk": {"status": "ok", "free_gb": 100, "used_pct": 10},
        "duckdb_capacity": {},
        "sources_config": [],
        "failed_syncs": [],
        "failed_dbt": [],
    }
    with ExitStack() as stack:
        enter = stack.enter_context
        for mod in ("health", "monitoring", "catalog"):
            enter(patch(f"dango.web.routes.{mod}.get_project_root", return_value=tmp_path))
        enter(patch("dango.web.routes.health.get_duckdb_path", return_value=tmp_path / "x.duckdb"))
        enter(
            patch(
                "dango.web.routes.health.check_service_status_async",
                new=AsyncMock(return_value="running"),
            )
        )
        enter(
            patch(
                "dango.web.routes.health.get_platform_health_data",
                new=AsyncMock(return_value=platform_data),
            )
        )
        enter(
            patch("dango.web.routes.health._get_scheduler_status", return_value={"running": True})
        )
        enter(
            patch(
                "dango.web.routes.health._get_cloud_health_data", new=AsyncMock(return_value=None)
            )
        )
        enter(
            patch(
                "dango.platform.watcher_lifecycle.get_watcher_status",
                return_value={"running": False, "log_file": None},
            )
        )
        enter(patch("dango.analysis.metrics.run_analysis", return_value=[]))
        enter(patch("dango.web.routes.monitoring._read_dbt_test_results", return_value=[]))
        enter(
            patch(
                "dango.web.routes.catalog._validate_and_resolve",
                new=AsyncMock(return_value=tmp_path / "x.duckdb"),
            )
        )
        enter(patch("dango.web.routes.catalog.profile_table", return_value={}))
        enter(patch("dango.web.routes.catalog._get_column_schema", return_value=[]))
        enter(patch("dango.web.routes.catalog._get_row_count", return_value=0))
        enter(patch("dango.web.routes.catalog._get_profiled_at", return_value=None))
        yield


def _client(project: AuthProject, role: Role | None) -> TestClient:
    client = build_client(project.root, health_router, monitoring_router, catalog_router)
    if role is not None:
        user = project.add_user(f"{role.value}@example.com", role)
        client.headers["Cookie"] = project.session_cookie(user)
    return client


def _call(client: TestClient, route: tuple[str, str]) -> Any:
    method, path = route
    return client.request(method, path, headers=_CSRF)


@pytest.mark.unit
class TestRoleMatrix:
    @pytest.mark.parametrize("route", HEALTH_ROUTES)
    @pytest.mark.parametrize("role", list(Role))
    def test_health_routes_open_to_every_role(
        self, project: AuthProject, role: Role, route: tuple[str, str]
    ) -> None:
        assert _call(_client(project, role), route).status_code == HEALTH_EXPECTED[role]

    @pytest.mark.parametrize("route", [MONITORING_RUN, TABLE_PROFILE])
    @pytest.mark.parametrize("role", list(Role))
    def test_mutating_routes_need_dbt_run(
        self, project: AuthProject, role: Role, route: tuple[str, str]
    ) -> None:
        assert _call(_client(project, role), route).status_code == MUTATING_EXPECTED[role]

    @pytest.mark.parametrize("route", ALL_ROUTES)
    def test_anonymous_is_401(self, project: AuthProject, route: tuple[str, str]) -> None:
        assert _call(_client(project, None), route).status_code == 401


@pytest.mark.unit
class TestAuthDisabled:
    @pytest.mark.parametrize("route", ALL_ROUTES)
    def test_disabled_local_anonymous_not_401(
        self, project: AuthProject, route: tuple[str, str]
    ) -> None:
        project.write_toggle("enabled: false\n")

        resp = _call(_client(project, None), route)

        assert resp.status_code == 200
