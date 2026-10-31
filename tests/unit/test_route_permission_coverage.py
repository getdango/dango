"""tests/unit/test_route_permission_coverage.py

Every route of the real app has a permission check or an explained ALLOWLIST entry; admin-only routes refuse Viewer/Editor.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts

from dango.auth.models import Role
from dango.web.app import app
from dango.web.routes.health import router as health_router
from dango.web.routes.secrets import router as secrets_router
from dango.web.routes.telemetry import router as telemetry_router
from dango.web.routes.upload import router as upload_router
from dango.web.routes.users import router as users_router
from tests.factories.auth_app_factories import AuthProject, build_client

_ANY = "*"

# (method or "*", path) -> why this route may lack a require_permission check.
# Methods are matched per route: a route registered for several methods needs "*" or one entry each.
ALLOWLIST: dict[tuple[str, str], str] = {
    # --- pre-login by design (also in middleware _PUBLIC_EXACT / _PUBLIC_PREFIXES) ---
    ("POST", "/api/auth/login"): "pre-login: credentials are the check",
    ("POST", "/api/auth/2fa/verify"): "pre-login: second step of login, partial session",
    ("POST", "/api/auth/accept-invite"): "pre-login: invite token is the check",
    ("GET", "/api/auth/oauth/{provider_name}/login"): "pre-login: starts an OAuth login",
    ("GET", "/api/auth/oauth/{provider_name}/callback"): "pre-login: completes an OAuth login",
    ("GET", "/login"): "pre-login: HTML shell",
    ("GET", "/setup"): "pre-login: first-run HTML shell",
    ("GET", "/invite/{token}"): "pre-login: invite HTML shell",
    (
        "*",
        "/api/health",
    ): "public liveness probe; also the Metabase API proxy catch for /api/health",
    # --- HTML shells and API docs: no data, the pages call permission-checked APIs ---
    ("GET", "/"): "HTML shell, redirects/renders UI only",
    ("GET", "/api"): "static API info document",
    ("GET", "/api/docs"): "Swagger UI shell (static HTML)",
    ("GET", "/api/redoc"): "ReDoc shell (static HTML)",
    ("GET", "/openapi.json"): "OpenAPI schema; describes routes, carries no project data",
    ("GET", "/health"): "HTML shell; data comes from permission-checked /api/health/platform",
    ("GET", "/logs"): "HTML shell; data comes from permission-checked /api/logs",
    ("GET", "/models"): "HTML shell; data comes from permission-checked /api/dbt/models",
    ("GET", "/sources"): "HTML shell; data comes from permission-checked /api/sources",
    ("GET", "/query"): "redirect to the catalog query tab",
    ("WS", "/static"): "static asset mount (CSS/JS/images), no project data",
    # --- own-account routes: the middleware authenticates, the handler scopes to request.state.user ---
    ("POST", "/api/auth/logout"): "own account: ends the caller's own session",
    ("GET", "/api/auth/me"): "own account: returns the caller",
    ("POST", "/api/auth/change-password"): "own account: requires the current password",
    ("GET", "/api/auth/sessions"): "own account: lists the caller's sessions (ownership tested)",
    ("DELETE", "/api/auth/sessions/{session_id}"): "own account: ownership checked (tested)",
    ("POST", "/api/auth/api-keys"): "own account: creates a key for the caller",
    ("GET", "/api/auth/api-keys"): "own account: lists the caller's keys",
    ("DELETE", "/api/auth/api-keys/{key_id}"): "own account: ownership checked (tested)",
    ("POST", "/api/auth/2fa/setup"): "own account: caller's 2FA enrolment",
    ("POST", "/api/auth/2fa/verify-setup"): "own account: caller's 2FA enrolment",
    ("POST", "/api/auth/2fa/disable"): "own account: requires the caller's password",
    ("POST", "/api/auth/2fa/regenerate-recovery"): "own account: requires the caller's password",
    ("GET", "/settings/account"): "own account: HTML shell for the caller's settings",
    # --- own authentication inside the handler ---
    ("POST", "/api/initial-sync/start"): "accepts deploy token or admin session (own check)",
    (
        "*",
        "/ws",
    ): "AuthMiddleware authenticates the connection (login and Origin) before the handler",
    ("POST", "/api/internal/schedules/reload"): "loopback-only guard in the handler (tested)",
    ("GET", "/api/internal/scheduler/status"): "loopback-only guard in the handler (tested)",
    # --- catch-all proxies: the upstream (Metabase / dbt docs) is reached only after AuthMiddleware ---
    (
        "*",
        "/metabase",
    ): "Metabase proxy; session required by middleware, Metabase enforces its own roles",
    ("*", "/metabase/{path:path}"): "Metabase proxy; same as /metabase",
    ("*", "/api/database"): "Metabase API proxy; same as /metabase",
    ("*", "/api/database/{path:path}"): "Metabase API proxy; same as /metabase",
    ("*", "/api/session"): "Metabase API proxy; same as /metabase",
    ("*", "/api/user"): "Metabase API proxy; same as /metabase",
    ("GET", "/app/{path:path}"): "Metabase static asset proxy",
    ("GET", "/public/{path:path}"): "Metabase static asset proxy",
    ("GET", "/styles.css"): "Metabase stylesheet proxy",
    ("GET", "/metabase/_dango/recovery.js"): "Static recovery script for the Metabase proxy",
    ("GET", "/dbt-docs"): "dbt docs static proxy; middleware requires a session",
    ("GET", "/dbt-docs/{path:path}"): "dbt docs static proxy; middleware requires a session",
    ("GET", "/manifest.json"): "dbt docs asset proxy; middleware requires a session",
    ("GET", "/catalog.json"): "dbt docs asset proxy; middleware requires a session",
}


def _walk(dependant: Dependant) -> Iterator[Dependant]:
    for sub in dependant.dependencies:
        yield sub
        yield from _walk(sub)


def _is_permission_dependency(dep: Dependant) -> bool:
    return "require_permission" in getattr(dep.call, "__qualname__", "")


def _has_permission_check(route: Any) -> bool:
    dependant = getattr(route, "dependant", None)
    if dependant is not None and any(_is_permission_dependency(d) for d in _walk(dependant)):
        return True
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:
        return False
    try:
        return "require_permission(" in inspect.getsource(endpoint)
    except (OSError, TypeError):
        return False


def _route_methods(route: Any) -> list[str]:
    methods = getattr(route, "methods", None)
    if methods:
        return sorted(m for m in methods if m not in {"HEAD", "OPTIONS"}) or ["GET"]
    return ["WS"]


def _unprotected_routes() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for ctx in iter_route_contexts(app.routes):
        route = ctx.route
        if _has_permission_check(route):
            continue
        for method in _route_methods(route):
            found.append((method, route.path))
    return found


def _allowlisted(method: str, path: str) -> bool:
    return (method, path) in ALLOWLIST or (_ANY, path) in ALLOWLIST


@pytest.mark.unit
class TestRoutePermissionCoverage:
    def test_every_route_has_permission_or_allowlist_entry(self) -> None:
        missing = [(m, p) for m, p in _unprotected_routes() if not _allowlisted(m, p)]

        assert not missing, (
            "Routes without Depends(require_permission(...)) and not in ALLOWLIST: "
            f"{missing}. Add a permission check or an ALLOWLIST entry with a reason."
        )

    def test_allowlist_has_no_stale_entries(self) -> None:
        unprotected = set(_unprotected_routes())
        unprotected_paths = {p for _m, p in unprotected}
        stale = [
            key
            for key in ALLOWLIST
            if key not in unprotected and not (key[0] == _ANY and key[1] in unprotected_paths)
        ]

        assert not stale, f"ALLOWLIST entries that no longer match an unprotected route: {stale}"

    def test_allowlist_entries_have_reasons(self) -> None:
        assert all(reason.strip() for reason in ALLOWLIST.values())

    def test_detector_sees_known_protected_and_unprotected_routes(self) -> None:
        """Positive control: the detector classifies known routes both ways."""
        by_key = {
            (m, ctx.route.path): ctx.route
            for ctx in iter_route_contexts(app.routes)
            for m in _route_methods(ctx.route)
        }

        assert _has_permission_check(by_key[("GET", "/api/sources/{source_name}/csv-files")])
        assert _has_permission_check(by_key[("DELETE", "/api/sources/{source_name}/csv-files")])
        assert _has_permission_check(by_key[("GET", "/api/deployments/history")])
        assert not _has_permission_check(by_key[("POST", "/api/auth/login")])


_ADMIN_ONLY_ROUTES: list[tuple[str, str]] = [
    ("GET", "/api/secrets"),
    ("GET", "/api/secrets/oauth"),
    ("GET", "/api/telemetry/status"),
    ("GET", "/api/admin/users"),
    ("GET", "/settings/telemetry"),
    ("GET", "/api/deployments/history"),
]


@pytest.mark.unit
class TestAdminOnlyRoleMatrix:
    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
        p = AuthProject(tmp_path)
        p.create_db()
        p.write_toggle("enabled: true\n")
        return p

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR])
    @pytest.mark.parametrize(("method", "path"), _ADMIN_ONLY_ROUTES)
    def test_viewer_and_editor_get_403(
        self, project: AuthProject, role: Role, method: str, path: str
    ) -> None:
        user = project.add_user(f"{role.value}@example.com", role)
        client = build_client(
            project.root, secrets_router, telemetry_router, users_router, health_router
        )

        resp = client.request(method, path, headers={"Cookie": project.session_cookie(user)})

        assert resp.status_code == 403

    @pytest.mark.parametrize(("method", "path"), _ADMIN_ONLY_ROUTES)
    def test_anonymous_gets_401(self, project: AuthProject, method: str, path: str) -> None:
        client = build_client(
            project.root, secrets_router, telemetry_router, users_router, health_router
        )

        resp = client.request(method, path)

        assert resp.status_code == 401


@pytest.mark.unit
class TestCsvFilesPermission:
    _PATH = "/api/sources/events/csv-files"

    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        p = AuthProject(tmp_path)
        p.create_db()
        p.write_toggle("enabled: true\n")
        return p

    def test_anonymous_gets_401(self, project: AuthProject) -> None:
        resp = build_client(project.root, upload_router).get(self._PATH)

        assert resp.status_code == 401

    def test_viewer_still_passes_the_permission_check(self, project: AuthProject) -> None:
        """Pin: Viewer holds source.view, so the route reaches its handler (no sources.yml -> 404)."""
        user = project.add_user("viewer@example.com", Role.VIEWER)
        with patch("dango.web.routes.upload.get_project_root", return_value=project.root):
            resp = build_client(project.root, upload_router).get(
                self._PATH, headers={"Cookie": project.session_cookie(user)}
            )

        assert resp.status_code == 404
