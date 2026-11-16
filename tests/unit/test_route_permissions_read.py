"""tests/unit/test_route_permissions_read.py

Read-only routes carry an explicit permission the Viewer role already holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dango.auth import permissions
from dango.auth.models import Role
from dango.web.app import app as dango_app
from dango.web.routes import config, dbt, logs, sources
from tests.factories.auth_app_factories import AuthProject, build_client

# (path, permission the route must require)
ROUTES: list[tuple[str, str]] = [
    ("/api/config", "dashboard.view"),
    ("/api/metabase-config", "dashboard.view"),
    ("/api/logs", "logs.view"),
    ("/api/sources/events/logs", "logs.view"),
    ("/api/sources", "source.view"),
    ("/api/sources/events/details", "source.view"),
    ("/api/dbt/models", "dbt.view"),
]
ROUTE_IDS = [path for path, _ in ROUTES]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[AuthProject]:
    proj = AuthProject(tmp_path)
    proj.create_db()
    (tmp_path / ".dango" / "sources.yml").write_text(
        "sources:\n  - name: events\n    type: csv\n    enabled: true\n"
    )
    monkeypatch.setattr(dango_app.state, "project_root", tmp_path, raising=False)
    yield proj


def _client(project: AuthProject) -> TestClient:
    return build_client(project.root, config.router, logs.router, sources.router, dbt.router)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR, Role.ADMIN])
@pytest.mark.parametrize("path", ROUTE_IDS)
def test_role_can_read(project: AuthProject, path: str, role: Role) -> None:
    project.write_toggle("enabled: true\n")
    user = project.add_user(f"{role.value}@example.com", role)
    client = _client(project)
    resp = client.get(path, headers={"Cookie": project.session_cookie(user)})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("path", ROUTE_IDS)
def test_anonymous_rejected_when_auth_enabled(project: AuthProject, path: str) -> None:
    project.write_toggle("enabled: true\n")
    assert _client(project).get(path).status_code == 401


@pytest.mark.parametrize("path,permission", ROUTES, ids=ROUTE_IDS)
def test_role_without_permission_gets_403(
    project: AuthProject, monkeypatch: pytest.MonkeyPatch, path: str, permission: str
) -> None:
    project.write_toggle("enabled: true\n")
    user = project.add_user("viewer@example.com", Role.VIEWER)
    reduced = dict(permissions.ROLE_PERMISSIONS)
    reduced[Role.VIEWER] = permissions.ROLE_PERMISSIONS[Role.VIEWER] - {permission}
    monkeypatch.setattr(permissions, "ROLE_PERMISSIONS", reduced)
    resp = _client(project).get(path, headers={"Cookie": project.session_cookie(user)})
    assert resp.status_code == 403


@pytest.mark.parametrize("path", ROUTE_IDS)
def test_auth_explicitly_disabled_local_anonymous_ok(project: AuthProject, path: str) -> None:
    project.write_toggle("enabled: false\n")
    resp = _client(project).get(path)
    assert resp.status_code == 200, resp.text
