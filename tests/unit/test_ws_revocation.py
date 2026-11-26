"""tests/unit/test_ws_revocation.py

An open /ws connection ends when the session or API key that opened it stops being valid.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dango.auth import database as auth_db
from dango.auth import sessions
from dango.auth.models import Role, User, UserUpdate
from dango.auth.security import hash_password
from dango.web.routes import websocket as ws_module
from dango.web.routes.auth import router as auth_router
from dango.web.routes.users import router as users_router
from dango.web.routes.websocket import router as ws_router
from tests.factories.auth_app_factories import AuthProject, build_client

_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_FAST = 0.01  # seconds between credential re-checks in polling tests
_NEVER = 3600.0  # polling cannot help: only an explicit in-process signal can close


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
    ws_module.ws_manager.__dict__.update(ws_module.ConnectionManager().__dict__)
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    return p


@pytest.fixture
def client(project: AuthProject) -> Iterator[TestClient]:
    """Real AuthMiddleware in front of the real auth, users and /ws routers."""
    with build_client(project.root, ws_router, auth_router, users_router) as c:
        yield c


def _interval(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    monkeypatch.setattr(ws_module, "WS_REVALIDATE_INTERVAL_SECONDS", seconds, raising=False)


def _wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return condition()


def _user(
    project: AuthProject, email: str, role: Role = Role.VIEWER, password_hash: str = "unused-hash"
) -> User:
    user = User(email=email, role=role, password_hash=password_hash)
    auth_db.create_user(project.db_path, user)
    return user


def _login(project: AuthProject, user: User) -> tuple[str, str]:
    """(cookie header value, session id) for a fresh session of *user*."""
    token, session = sessions.create_session(project.db_path, user.id)
    return f"dango_session={token}", session.id


def _mgr() -> ws_module.ConnectionManager:
    return ws_module.ws_manager


def _assert_alive(ws: Any) -> None:
    ws.send_text("ping")
    assert ws.receive_json()["event"] == "echo"


def _assert_closed_by_server(ws: Any) -> None:
    assert _wait_until(lambda: not _mgr().active_connections), "connection stayed open"
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    assert exc.value.code == 1008
    assert _mgr()._per_identity == {}
    assert _mgr()._identity_of == {}
    assert _mgr()._wake == {}


def _connect(client: TestClient, cookie: str | None = None, bearer: str | None = None) -> Any:
    headers = {"cookie": cookie} if cookie else {"authorization": f"Bearer {bearer}"}
    return client.websocket_connect("/ws", headers=headers)


def _broadcast(client: TestClient, message: dict[str, Any]) -> None:
    async def run() -> None:
        await _mgr().broadcast(message, log=False)

    cast(Any, client).portal.call(run)


@pytest.mark.unit
class TestInProcessEndingClosesConnection:
    """Polling is switched off: the in-process handlers must close the connection themselves."""

    @pytest.fixture(autouse=True)
    def _no_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _interval(monkeypatch, _NEVER)

    def test_logout(self, project: AuthProject, client: TestClient) -> None:
        cookie, _sid = _login(project, _user(project, "a@example.com"))
        with _connect(client, cookie) as ws:
            assert ws.receive_json()["event"] == "connected"

            assert (
                client.post("/api/auth/logout", headers={**_CSRF, "cookie": cookie}).status_code
                == 200
            )

            _assert_closed_by_server(ws)

    def test_revoke_one_session_closes_only_that_sessions_connection(
        self, project: AuthProject, client: TestClient
    ) -> None:
        user = _user(project, "a@example.com")
        cookie_a, sid_a = _login(project, user)
        cookie_b, _sid_b = _login(project, user)
        with _connect(client, cookie_a) as ws_a, _connect(client, cookie_b) as ws_b:
            ws_a.receive_json()
            ws_b.receive_json()

            resp = client.delete(
                f"/api/auth/sessions/{sid_a}", headers={**_CSRF, "cookie": cookie_b}
            )

            assert resp.status_code == 200
            assert _wait_until(lambda: len(_mgr().active_connections) == 1)
            _assert_alive(ws_b)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws_a.receive_json()
            assert exc.value.code == 1008

    def test_api_key_revocation_closes_key_connection_not_cookie_connection(
        self, project: AuthProject, client: TestClient
    ) -> None:
        user = _user(project, "a@example.com")
        cookie, _sid = _login(project, user)
        raw_key, key = sessions.create_api_key(project.db_path, user.id, "k")
        with _connect(client, bearer=raw_key) as ws_key, _connect(client, cookie) as ws_cookie:
            ws_key.receive_json()
            ws_cookie.receive_json()

            resp = client.delete(
                f"/api/auth/api-keys/{key.id}", headers={**_CSRF, "cookie": cookie}
            )

            assert resp.status_code == 200
            assert _wait_until(lambda: len(_mgr().active_connections) == 1)
            _assert_alive(ws_cookie)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws_key.receive_json()
            assert exc.value.code == 1008

    def test_change_password(self, project: AuthProject, client: TestClient) -> None:
        user = _user(
            project, "a@example.com", password_hash=hash_password("Old-passw0rd-for-tests!")
        )
        cookie, _sid = _login(project, user)
        with _connect(client, cookie) as ws:
            ws.receive_json()

            resp = client.post(
                "/api/auth/change-password",
                headers={**_CSRF, "cookie": cookie},
                json={
                    "current_password": "Old-passw0rd-for-tests!",
                    "new_password": "Brand-new-passw0rd-9731",
                },
            )

            assert resp.status_code == 200
            _assert_closed_by_server(ws)

    @pytest.mark.parametrize("action", ["deactivate", "revoke-sessions"])
    def test_admin_action_closes_target_not_admin_or_others(
        self, project: AuthProject, client: TestClient, action: str
    ) -> None:
        admin = _user(project, "admin@example.com", Role.ADMIN)
        other = _user(project, "other@example.com")
        admin_cookie, _ = _login(project, admin)
        target = _user(project, "target@example.com")
        target_cookie, _ = _login(project, target)
        other_cookie, _ = _login(project, other)
        with (
            _connect(client, target_cookie) as ws_t,
            _connect(client, admin_cookie) as ws_admin,
            _connect(client, other_cookie) as ws_o,
        ):
            for ws in (ws_t, ws_admin, ws_o):
                ws.receive_json()

            resp = client.post(
                f"/api/admin/users/{target.id}/{action}", headers={**_CSRF, "cookie": admin_cookie}
            )

            assert resp.status_code == 200
            assert _wait_until(lambda: len(_mgr().active_connections) == 2)
            _broadcast(client, {"event": "probe"})
            assert ws_admin.receive_json()["event"] == "probe"
            assert ws_o.receive_json()["event"] == "probe"
            with pytest.raises(WebSocketDisconnect) as exc:
                ws_t.receive_json()
            assert exc.value.code == 1008
            assert sorted(_mgr()._per_identity) == sorted([f"user:{admin.id}", f"user:{other.id}"])

    def test_role_downgrade_keeps_connection(
        self, project: AuthProject, client: TestClient
    ) -> None:
        """/ws is not permission-gated: any authenticated role may listen, so a role change is not a revocation."""
        admin_cookie, _ = _login(project, _user(project, "admin@example.com", Role.ADMIN))
        target = _user(project, "target@example.com", Role.EDITOR)
        target_cookie, _ = _login(project, target)
        with _connect(client, target_cookie) as ws:
            ws.receive_json()

            resp = client.put(
                f"/api/admin/users/{target.id}/role",
                headers={**_CSRF, "cookie": admin_cookie},
                json={"role": "viewer"},
            )

            assert resp.status_code == 200
            _assert_alive(ws)
            assert len(_mgr().active_connections) == 1


@pytest.mark.unit
class TestPollingClosesConnection:
    """Endings the web process is not told about (the CLI, expiry) are caught by the re-check."""

    @pytest.fixture(autouse=True)
    def _fast_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _interval(monkeypatch, _FAST)

    def test_session_invalidated_in_database(
        self, project: AuthProject, client: TestClient
    ) -> None:
        cookie, sid = _login(project, _user(project, "a@example.com"))
        with _connect(client, cookie) as ws:
            ws.receive_json()

            sessions.invalidate_session(project.db_path, sid)

            _assert_closed_by_server(ws)

    def test_user_deactivated_in_database(self, project: AuthProject, client: TestClient) -> None:
        user = _user(project, "a@example.com")
        cookie, _sid = _login(project, user)
        with _connect(client, cookie) as ws:
            ws.receive_json()

            auth_db.update_user(project.db_path, user.id, UserUpdate(is_active=False))

            _assert_closed_by_server(ws)

    def test_api_key_revoked_in_database(self, project: AuthProject, client: TestClient) -> None:
        user = _user(project, "a@example.com")
        raw_key, key = sessions.create_api_key(project.db_path, user.id, "k")
        with _connect(client, bearer=raw_key) as ws:
            ws.receive_json()

            sessions.revoke_api_key(project.db_path, key.id)

            _assert_closed_by_server(ws)

    def test_recheck_runs_repeatedly_without_refreshing_the_session(
        self, project: AuthProject, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An open socket must not keep an idle session alive: the re-check does not refresh activity."""
        cookie, _sid = _login(project, _user(project, "a@example.com"))
        real = sessions.validate_session
        calls: list[dict[str, Any]] = []

        def spy(*args: Any, **kwargs: Any) -> User | None:
            calls.append(kwargs)
            return real(*args, **kwargs)

        with _connect(client, cookie) as ws:
            ws.receive_json()
            monkeypatch.setattr(sessions, "validate_session", spy)

            assert _wait_until(lambda: len(calls) >= 3)
            assert all(c.get("refresh_activity") is False for c in calls)
            _assert_alive(ws)

    def test_recheck_does_not_record_api_key_use(
        self, project: AuthProject, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user = _user(project, "a@example.com")
        raw_key, key = sessions.create_api_key(project.db_path, user.id, "k")
        real = sessions.validate_api_key
        calls: list[int] = []

        def spy(*args: Any, **kwargs: Any) -> User | None:
            calls.append(1)
            return real(*args, **kwargs)

        with _connect(client, bearer=raw_key) as ws:
            ws.receive_json()
            used_at = auth_db.get_api_key_by_hash(project.db_path, key.key_hash)
            assert used_at is not None
            monkeypatch.setattr(sessions, "validate_api_key", spy)

            assert _wait_until(lambda: len(calls) >= 3)
            after = auth_db.get_api_key_by_hash(project.db_path, key.key_hash)
            assert after is not None
            assert after.last_used_at == used_at.last_used_at

    def test_connection_closes_once_the_database_recovers(
        self, project: AuthProject, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cookie, sid = _login(project, _user(project, "a@example.com"))
        real = sessions.validate_session

        def broken(*args: Any, **kwargs: Any) -> User | None:
            raise sqlite3.OperationalError("database is locked")

        with _connect(client, cookie) as ws:
            ws.receive_json()
            monkeypatch.setattr(sessions, "validate_session", broken)
            sessions.invalidate_session(project.db_path, sid)
            time.sleep(0.1)
            assert len(_mgr().active_connections) == 1  # an unreadable database closes nothing

            monkeypatch.setattr(sessions, "validate_session", real)

            _assert_closed_by_server(ws)


@pytest.mark.unit
def test_default_recheck_interval_is_bounded() -> None:
    assert 1.0 <= ws_module.WS_REVALIDATE_INTERVAL_SECONDS <= 30.0


@pytest.mark.unit
class TestNegativeControls:
    @pytest.fixture(autouse=True)
    def _fast_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _interval(monkeypatch, _FAST)

    def test_other_users_connection_untouched_by_logout(
        self, project: AuthProject, client: TestClient
    ) -> None:
        cookie_a, _ = _login(project, _user(project, "a@example.com"))
        user_b = _user(project, "b@example.com")
        cookie_b, _ = _login(project, user_b)
        with _connect(client, cookie_a) as ws_a, _connect(client, cookie_b) as ws_b:
            ws_a.receive_json()
            ws_b.receive_json()

            client.post("/api/auth/logout", headers={**_CSRF, "cookie": cookie_a})
            time.sleep(0.2)  # about twenty re-check intervals

            _broadcast(client, {"event": "probe"})
            assert ws_b.receive_json()["event"] == "probe"
            assert f"user:{user_b.id}" in _mgr()._per_identity

    def test_same_users_other_session_untouched_by_logout(
        self, project: AuthProject, client: TestClient
    ) -> None:
        user = _user(project, "a@example.com")
        cookie_1, _ = _login(project, user)
        cookie_2, _ = _login(project, user)
        with _connect(client, cookie_1) as ws_1, _connect(client, cookie_2) as ws_2:
            ws_1.receive_json()
            ws_2.receive_json()

            client.post("/api/auth/logout", headers={**_CSRF, "cookie": cookie_1})
            time.sleep(0.2)  # about twenty re-check intervals

            _assert_alive(ws_2)
            assert f"user:{user.id}" in _mgr()._per_identity

    def test_auth_disabled_local_connection_is_never_closed(
        self, project: AuthProject, client: TestClient
    ) -> None:
        project.write_toggle("enabled: false\n")
        with client.websocket_connect("/ws", headers={"host": "localhost:8800"}) as ws:
            ws.receive_json()

            time.sleep(0.2)  # about twenty re-check intervals

            _assert_alive(ws)
            assert _mgr()._per_identity == {"local-auth-disabled": 1}

    def test_database_error_in_recheck_keeps_connection(
        self, project: AuthProject, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cookie, _sid = _login(project, _user(project, "a@example.com"))

        def broken(*args: Any, **kwargs: Any) -> User | None:
            raise sqlite3.OperationalError("database is locked")

        with _connect(client, cookie) as ws:
            ws.receive_json()
            monkeypatch.setattr(sessions, "validate_session", broken)

            time.sleep(0.2)  # about twenty re-check intervals

            _assert_alive(ws)
            assert len(_mgr().active_connections) == 1

    def test_missing_auth_database_keeps_connection_and_does_not_recreate_it(
        self, project: AuthProject, client: TestClient
    ) -> None:
        cookie, _sid = _login(project, _user(project, "a@example.com"))
        with _connect(client, cookie) as ws:
            ws.receive_json()
            for suffix in ("", "-wal", "-shm"):
                Path(f"{project.db_path}{suffix}").unlink(missing_ok=True)

            time.sleep(0.2)

            _assert_alive(ws)
            assert not project.db_path.exists()
