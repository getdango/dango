"""tests/unit/test_route_account_ownership.py

Own-account routes refuse another user's session/API key; internal routes refuse proxied requests.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dango.auth import database as auth_db
from dango.auth.models import Role, User
from dango.web.routes.auth import router as auth_router
from dango.web.routes.schedules import router as schedules_router
from tests.factories.auth_app_factories import AuthProject, build_client

_CSRF = {"X-Requested-With": "XMLHttpRequest"}


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    return p


def _headers(project: AuthProject, user: User) -> dict[str, str]:
    return {**_CSRF, "Cookie": project.session_cookie(user)}


def _session_ids(project: AuthProject, user: User) -> set[str]:
    return {s.id for s in auth_db.list_user_sessions(project.db_path, user.id)}


def _key_ids(project: AuthProject, user: User) -> set[str]:
    return {k.id for k in auth_db.list_user_api_keys(project.db_path, user.id)}


@pytest.fixture(params=[Role.VIEWER, Role.ADMIN], ids=["same_role", "admin_attacker"])
def attacker_role(request: pytest.FixtureRequest) -> Role:
    role: Role = request.param
    return role


@pytest.mark.unit
class TestCrossUserOwnership:
    def test_cannot_revoke_another_users_session(
        self, project: AuthProject, attacker_role: Role
    ) -> None:
        owner = project.add_user("owner@example.com", Role.VIEWER)
        attacker = project.add_user("attacker@example.com", attacker_role)
        project.session_cookie(owner)
        (owner_session,) = _session_ids(project, owner)
        client = build_client(project.root, auth_router)

        resp = client.delete(
            f"/api/auth/sessions/{owner_session}", headers=_headers(project, attacker)
        )

        assert resp.status_code == 404
        assert _session_ids(project, owner) == {owner_session}

    def test_cannot_revoke_another_users_api_key(
        self, project: AuthProject, attacker_role: Role
    ) -> None:
        owner = project.add_user("owner@example.com", Role.VIEWER)
        attacker = project.add_user("attacker@example.com", attacker_role)
        project.api_key(owner)
        (owner_key,) = _key_ids(project, owner)
        client = build_client(project.root, auth_router)

        resp = client.delete(f"/api/auth/api-keys/{owner_key}", headers=_headers(project, attacker))

        assert resp.status_code == 404
        assert _key_ids(project, owner) == {owner_key}

    def test_cannot_list_another_users_sessions(
        self, project: AuthProject, attacker_role: Role
    ) -> None:
        owner = project.add_user("owner@example.com", Role.VIEWER)
        attacker = project.add_user("attacker@example.com", attacker_role)
        project.session_cookie(owner)
        (owner_session,) = _session_ids(project, owner)
        client = build_client(project.root, auth_router)

        resp = client.get("/api/auth/sessions", headers=_headers(project, attacker))

        assert resp.status_code == 200
        assert owner_session not in {s["id"] for s in resp.json()}

    def test_owner_can_revoke_own_other_session(self, project: AuthProject) -> None:
        """Positive control: the ownership check does not block the legitimate owner."""
        owner = project.add_user("owner@example.com", Role.VIEWER)
        project.session_cookie(owner)
        (older,) = _session_ids(project, owner)
        client = build_client(project.root, auth_router)

        resp = client.delete(f"/api/auth/sessions/{older}", headers=_headers(project, owner))

        # The request above authenticates with a *new* session, so `older` is not current.
        assert resp.status_code == 200
        assert older not in _session_ids(project, owner)


@pytest.mark.unit
class TestInternalRoutesRefuseProxiedRequests:
    _ROUTES: list[tuple[str, str]] = [
        ("POST", "/api/internal/schedules/reload"),
        ("GET", "/api/internal/scheduler/status"),
    ]

    def _client(self, project: AuthProject) -> TestClient:
        return build_client(project.root, schedules_router)

    @pytest.mark.parametrize(("method", "path"), _ROUTES)
    def test_internal_schedule_routes_refuse_proxied_request(
        self, project: AuthProject, method: str, path: str
    ) -> None:
        resp = self._client(project).request(
            method, path, headers={**_CSRF, "X-Forwarded-For": "203.0.113.9"}
        )

        assert resp.status_code == 403
        assert resp.json() == {"error": "Localhost only"}

    @pytest.mark.parametrize(("method", "path"), _ROUTES)
    def test_internal_schedule_routes_accept_direct_loopback(
        self, project: AuthProject, method: str, path: str
    ) -> None:
        """Positive control: without X-Forwarded-For the same loopback client passes the guard."""
        resp: Any = self._client(project).request(method, path, headers=_CSRF)

        assert resp.status_code != 403
