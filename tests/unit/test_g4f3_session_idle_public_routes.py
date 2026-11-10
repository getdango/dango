"""tests/unit/test_g4f3_session_idle_public_routes.py

Public routes that read a session cookie themselves (GET /login, POST /api/initial-sync/start)
honour the configured idle timeout, never extend an idle session, and the initial-sync session
path needs the CSRF header, a full (not partial) session and an admin.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

import dango.web.routes.initial_sync as initial_sync_mod
from dango.auth import database as auth_db
from dango.auth import sessions
from dango.auth.models import Role, User
from dango.auth.security import hash_token
from dango.web.middleware.auth import COOKIE_NAME
from dango.web.routes.initial_sync import InitialSyncState
from dango.web.routes.initial_sync import router as initial_sync_router
from dango.web.routes.ui import router as ui_router
from tests.factories.auth_app_factories import AuthProject, build_client

_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_START = "/api/initial-sync/start"
_IDLE_MINUTES = 60


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    (tmp_path / ".dango" / "project.yml").write_text(
        f"auth:\n  idle_timeout_minutes: {_IDLE_MINUTES}\n"
    )
    return p


@pytest.fixture(autouse=True)
def _initial_state(project: AuthProject) -> Iterator[None]:
    initial_sync_mod._sync_state = InitialSyncState()
    empty = MagicMock()
    empty.sources.sources = []
    with (
        patch("dango.web.routes.initial_sync.get_project_root", return_value=project.root),
        patch("dango.config.helpers.load_config", return_value=empty),
    ):
        yield
    initial_sync_mod._sync_state = InitialSyncState()


def _token(cookie: str) -> str:
    return cookie.split("=", 1)[1]


def _set_last_activity(project: AuthProject, token: str, when: datetime) -> None:
    conn = auth_db._connect(project.db_path)
    try:
        conn.execute(
            "UPDATE sessions SET last_activity = ? WHERE token_hash = ?",
            (when.isoformat(), hash_token(token)),
        )
        conn.commit()
    finally:
        conn.close()


def _last_activity(project: AuthProject, token: str) -> datetime:
    session = auth_db.get_session_by_token(project.db_path, hash_token(token))
    assert session is not None
    return session.last_activity


def _session(project: AuthProject, role: Role, idle_minutes: int) -> tuple[User, str, datetime]:
    user = project.add_user(f"{role.value}@example.com", role)
    cookie = project.session_cookie(user)
    when = datetime.now(timezone.utc) - timedelta(minutes=idle_minutes)
    _set_last_activity(project, _token(cookie), when)
    return user, cookie, when


@pytest.mark.unit
class TestLoginPageIdle:
    def test_idle_expired_session_is_not_revived(self, project: AuthProject) -> None:
        _user, cookie, _when = _session(project, Role.VIEWER, idle_minutes=120)
        client = build_client(project.root, ui_router)

        resp = client.get("/login", headers={"Cookie": cookie}, follow_redirects=False)

        assert resp.status_code == 200  # the login page, not a redirect to "/"
        assert (
            sessions.validate_session(
                project.db_path, _token(cookie), idle_timeout_minutes=_IDLE_MINUTES
            )
            is None
        )

    def test_active_session_redirects_without_refreshing(self, project: AuthProject) -> None:
        _user, cookie, when = _session(project, Role.VIEWER, idle_minutes=10)
        client = build_client(project.root, ui_router)

        resp = client.get("/login", headers={"Cookie": cookie}, follow_redirects=False)

        assert resp.status_code == 302
        assert resp.headers["location"] == "/"
        assert _last_activity(project, _token(cookie)) == when


@pytest.mark.unit
class TestInitialSyncSessionPath:
    def _post(self, project: AuthProject, cookie: str, csrf: bool = True) -> httpx.Response:
        headers = {"Cookie": cookie, **(_CSRF if csrf else {})}
        return build_client(project.root, initial_sync_router).post(_START, headers=headers)

    def test_fresh_admin_session_with_csrf_header_is_accepted(self, project: AuthProject) -> None:
        _user, cookie, _when = _session(project, Role.ADMIN, idle_minutes=5)
        resp = self._post(project, cookie)
        assert resp.status_code == 200
        assert resp.json()["status"] == "skipped"

    def test_idle_expired_admin_session_is_refused_and_not_revived(
        self, project: AuthProject
    ) -> None:
        _user, cookie, _when = _session(project, Role.ADMIN, idle_minutes=120)
        resp = self._post(project, cookie)
        assert resp.status_code == 401
        assert (
            sessions.validate_session(
                project.db_path, _token(cookie), idle_timeout_minutes=_IDLE_MINUTES
            )
            is None
        )

    def test_missing_csrf_header_is_refused(self, project: AuthProject) -> None:
        _user, cookie, _when = _session(project, Role.ADMIN, idle_minutes=5)
        resp = self._post(project, cookie, csrf=False)
        assert resp.status_code == 401
        assert initial_sync_mod._sync_state.phase.value == "idle"

    def test_partial_session_is_refused(self, project: AuthProject) -> None:
        user = project.add_user("admin@example.com", Role.ADMIN)
        token, _session_row = sessions.create_session(project.db_path, user.id, is_partial=True)
        resp = self._post(project, f"{COOKIE_NAME}={token}")
        assert resp.status_code == 401

    def test_non_admin_does_not_refresh_activity(self, project: AuthProject) -> None:
        _user, cookie, when = _session(project, Role.VIEWER, idle_minutes=10)
        resp = self._post(project, cookie)
        assert resp.status_code == 401
        assert _last_activity(project, _token(cookie)) == when
