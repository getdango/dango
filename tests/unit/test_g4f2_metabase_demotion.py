"""tests/unit/test_g4f2_metabase_demotion.py

A role change reports success only when every Metabase step succeeded; a failed demotion
suspends the user's Metabase access until it is re-applied, and every role change deletes the
user's known Metabase sessions.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.auth import database as db
from dango.auth.metabase_bridge import bridge_metabase_login
from dango.auth.metabase_sync import sync_user_role
from dango.auth.models import Role, User
from dango.migrations.runner import MigrationRunner

MB = "http://localhost:3000"
_AJAX = {"X-Requested-With": "XMLHttpRequest"}


def _resp(status: int = 200, data: Any = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {} if data is None else data
    return r


class FakeMetabase:
    """Routes the ``requests`` calls of metabase_sync; ``fail`` entries answer HTTP 500."""

    def __init__(self, group_ids: list[int], fail: tuple[tuple[str, str], ...] = ()) -> None:
        self.group_ids = group_ids
        self.fail = set(fail)
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def _answer(self, method: str, url: str, headers: dict[str, str] | None) -> MagicMock:
        path = url.removeprefix(MB)
        self.calls.append((method, path, dict(headers or {})))
        if any(m == method and needle in path for m, needle in self.fail):
            return _resp(500)
        if method == "POST" and path == "/api/session":
            return _resp(200, {"id": "admin-session"})
        if method == "GET" and path == "/api/permissions/group":
            return _resp(200, [{"id": 1, "name": "All Users"}, {"id": 5, "name": "Dango Editors"}])
        if method == "GET" and path == "/api/permissions/graph":
            return _resp(200, {"groups": {}})
        if method == "GET" and path == "/api/user/10":
            return _resp(200, {"id": 10, "group_ids": self.group_ids})
        if method == "GET" and path.startswith("/api/permissions/group/"):
            gid = int(path.rsplit("/", 1)[1])
            return _resp(200, {"members": [{"user_id": 10, "membership_id": gid * 10}]})
        if method == "DELETE":
            return _resp(204)
        return _resp(200, {})

    def install(self, requests_mock: MagicMock) -> None:
        requests_mock.get.side_effect = lambda url, headers=None, **kw: self._answer(
            "GET", url, headers
        )
        requests_mock.post.side_effect = lambda url, headers=None, **kw: self._answer(
            "POST", url, headers
        )
        requests_mock.put.side_effect = lambda url, headers=None, **kw: self._answer(
            "PUT", url, headers
        )
        requests_mock.delete.side_effect = lambda url, headers=None, **kw: self._answer(
            "DELETE", url, headers
        )

    def deleted_sessions(self) -> list[str]:
        return [
            h.get("X-Metabase-Session", "")
            for m, p, h in self.calls
            if m == "DELETE" and p == "/api/session"
        ]


def _project(tmp_path: Path, role: Role, **overrides: Any) -> tuple[Path, User]:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    migrations = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations).apply_pending()
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": MB,
                "admin": {"email": "admin@test.com", "password": "testpassword123"},
                "database": {"id": 1, "name": "Test"},
            }
        )
    )
    fields: dict[str, Any] = {
        "email": "demoted@example.com",
        "password_hash": "$2b$12$fake",
        "role": role,
        "metabase_user_id": 10,
        "metabase_password_enc": "enc",
    }
    fields.update(overrides)
    user = User(**fields)
    db.create_user(db_path, user)
    return db_path, user


@pytest.mark.unit
class TestEveryStepIsChecked:
    @pytest.mark.parametrize(
        ("group_ids", "fail"),
        [
            ([1, 2, 5], (("PUT", "/api/user/10"),)),
            ([1, 2, 5], (("DELETE", "/api/permissions/membership/"),)),
            ([1, 5], (("GET", "/api/permissions/group/5"),)),
            ([1, 5], (("GET", "/api/user/10"),)),
        ],
    )
    def test_failed_demotion_step_is_reported(
        self, tmp_path: Path, group_ids: list[int], fail: tuple[tuple[str, str], ...]
    ) -> None:
        db_path, user = _project(tmp_path, Role.VIEWER)
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            FakeMetabase(group_ids, fail).install(requests_mock)
            assert sync_user_role(db_path, user.id, tmp_path, MB) is False

    def test_failed_promotion_membership_is_reported(self, tmp_path: Path) -> None:
        db_path, user = _project(tmp_path, Role.EDITOR)
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            FakeMetabase([1], (("POST", "/api/permissions/membership"),)).install(requests_mock)
            assert sync_user_role(db_path, user.id, tmp_path, MB) is False

    def test_complete_demotion_reports_success(self, tmp_path: Path) -> None:
        db_path, user = _project(tmp_path, Role.VIEWER)
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            fake = FakeMetabase([1, 2, 5])
            fake.install(requests_mock)
            assert sync_user_role(db_path, user.id, tmp_path, MB) is True
        assert ("PUT", "/api/user/10") in [(m, p) for m, p, _ in fake.calls]


def _bridge(user: User, root: Path, db_path: Path, session_id: str = "mb-sess-1") -> str | None:
    client = AsyncMock()
    client.post.return_value = _resp(200, {"id": session_id})
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    with (
        patch("dango.auth.metabase_bridge.httpx.AsyncClient", return_value=client),
        patch("dango.auth.metabase_sync.decrypt_metabase_password", return_value="pw"),
    ):
        return asyncio.run(bridge_metabase_login(user, root, MB, db_path=db_path))


@pytest.mark.unit
class TestSessionsAndSuspension:
    def test_role_change_deletes_the_users_bridged_sessions(self, tmp_path: Path) -> None:
        db_path, user = _project(tmp_path, Role.VIEWER)
        assert _bridge(user, tmp_path, db_path, "mb-sess-1") == "mb-sess-1"
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            fake = FakeMetabase([1, 2, 5])
            fake.install(requests_mock)
            assert sync_user_role(db_path, user.id, tmp_path, MB) is True
        assert fake.deleted_sessions() == ["mb-sess-1"]

    def test_failed_demotion_suspends_bridging_until_reapplied(self, tmp_path: Path) -> None:
        db_path, user = _project(tmp_path, Role.VIEWER)
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            FakeMetabase([1, 2, 5], (("PUT", "/api/user/10"),)).install(requests_mock)
            assert sync_user_role(db_path, user.id, tmp_path, MB) is False
        assert _bridge(user, tmp_path, db_path) is None
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            FakeMetabase([1, 2, 5]).install(requests_mock)
            assert sync_user_role(db_path, user.id, tmp_path, MB) is True
        assert _bridge(user, tmp_path, db_path, "mb-sess-2") == "mb-sess-2"

    def test_proxy_refuses_a_user_with_an_unapplied_role_change(self, tmp_path: Path) -> None:
        from dango.web.routes.metabase_proxy import router as proxy_router

        db_path, user = _project(tmp_path, Role.VIEWER)
        with patch("dango.auth.metabase_sync.requests") as requests_mock:
            FakeMetabase([1, 2, 5], (("PUT", "/api/user/10"),)).install(requests_mock)
            sync_user_role(db_path, user.id, tmp_path, MB)

        app = FastAPI()
        app.state.project_root = tmp_path
        app.state.http_client = AsyncMock()
        app.include_router(proxy_router)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = user
            return await call_next(request)

        resp = TestClient(app, raise_server_exceptions=False).get(
            "/metabase/api/user/current", headers={"Cookie": "metabase.SESSION=old-privileged"}
        )
        assert resp.status_code == 403
        app.state.http_client.request.assert_not_called()
        app.state.http_client.send.assert_not_called()


@pytest.mark.unit
class TestRoleRouteReportsIncompleteChange:
    def _client(self, tmp_path: Path) -> tuple[TestClient, Path, User]:
        from dango.web.routes.users import router

        db_path, target = _project(tmp_path, Role.ADMIN)
        admin = User(email="boss@example.com", password_hash="$2b$12$fake", role=Role.ADMIN)
        db.create_user(db_path, admin)
        app = FastAPI()
        app.state.project_root = tmp_path
        app.include_router(router)

        @app.middleware("http")
        async def set_user(request: Any, call_next: Any) -> Any:
            request.state.user = admin
            request.state.auth_method = "session"
            return await call_next(request)

        return TestClient(app, raise_server_exceptions=False), db_path, target

    def test_incomplete_metabase_update_is_reported(self, tmp_path: Path) -> None:
        client, db_path, target = self._client(tmp_path)
        with patch("dango.auth.metabase_sync.sync_user_role", return_value=False):
            resp = client.put(
                f"/api/admin/users/{target.id}/role", json={"role": "viewer"}, headers=_AJAX
            )
        assert resp.status_code == 502
        assert "Metabase" in resp.json()["message"]
        changed = db.get_user_by_id(db_path, target.id)
        assert changed is not None and changed.role == Role.VIEWER

    def test_complete_update_is_a_plain_success(self, tmp_path: Path) -> None:
        client, _db_path, target = self._client(tmp_path)
        with patch("dango.auth.metabase_sync.sync_user_role", return_value=True):
            resp = client.put(
                f"/api/admin/users/{target.id}/role", json={"role": "viewer"}, headers=_AJAX
            )
        assert resp.status_code == 200


@pytest.mark.unit
class TestCliChangeRoleWarns:
    def test_incomplete_metabase_update_is_not_reported_as_complete(self, tmp_path: Path) -> None:
        from dango.cli.main import cli

        db_path, target = _project(tmp_path, Role.EDITOR)
        other = User(email="other@example.com", password_hash="$2b$12$fake", role=Role.ADMIN)
        db.create_user(db_path, other)
        with (
            patch("dango.cli.utils.find_project_root", return_value=tmp_path),
            patch("dango.auth.metabase_sync.sync_user_role", return_value=False),
        ):
            result = CliRunner().invoke(cli, ["auth", "change-role", target.email, "viewer"])
        assert result.exit_code == 0
        assert "metabase" in result.output.lower()
        assert "incomplete" in result.output.lower()
