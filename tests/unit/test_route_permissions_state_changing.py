"""tests/unit/test_route_permissions_state_changing.py

Permission checks on POST /api/sources/{name}/sync and the /api/initial-sync/* routes, through the real AuthMiddleware.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import dango.web.routes.initial_sync as initial_sync_mod
from dango.auth.audit import AuditEvent
from dango.auth.models import Role
from dango.web.routes.initial_sync import InitialSyncState, SyncPhase
from dango.web.routes.initial_sync import router as initial_sync_router
from dango.web.routes.sync import router as sync_router
from tests.factories.auth_app_factories import AuthProject, build_client

_CSRF = {"X-Requested-With": "XMLHttpRequest"}
_SYNC = "/api/sources/events/sync"
_STATUS = "/api/initial-sync/status"
_SKIP = "/api/initial-sync/skip-source"
_CANCEL = "/api/initial-sync/cancel"
_START = "/api/initial-sync/start"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthProject:
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    return p


@pytest.fixture
def sync_task() -> Iterator[AsyncMock]:
    """Replace the background sync task and the source lookup; yields the task mock."""
    with (
        patch("dango.web.routes.sync.load_sources_config", return_value=[{"name": "events"}]),
        patch("dango.web.routes.sync.run_sync_task", new_callable=AsyncMock) as task,
    ):
        yield task


@pytest.fixture(autouse=True)
def audit_root(project: AuthProject, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point audit.jsonl at the test project."""
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(project.root))


def _audit(project: AuthProject) -> list[dict[str, Any]]:
    path = project.root / ".dango" / "logs" / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def initial_state(project: AuthProject) -> Iterator[InitialSyncState]:
    initial_sync_mod._sync_state = InitialSyncState(phase=SyncPhase.SYNCING)
    initial_sync_mod._sync_state.current_source_name = "events"
    with patch("dango.web.routes.initial_sync.get_project_root", return_value=project.root):
        yield initial_sync_mod._sync_state
    initial_sync_mod._sync_state = InitialSyncState()


def _sync_client(project: AuthProject) -> TestClient:
    return build_client(project.root, sync_router)


def _initial_client(project: AuthProject) -> TestClient:
    return build_client(project.root, initial_sync_router)


def _post_as(client: TestClient, project: AuthProject, role: Role, path: str, **kwargs: Any) -> Any:
    users: dict[str, Any] = project.__dict__.setdefault("_users", {})
    if role.value not in users:
        users[role.value] = project.add_user(f"{role.value}@example.com", role)
    user = users[role.value]
    headers = {**_CSRF, "Cookie": project.session_cookie(user)}
    return client.post(path, headers=headers, **kwargs)


@pytest.mark.unit
class TestSourceSyncPermission:
    @pytest.mark.parametrize("role", [Role.EDITOR, Role.ADMIN])
    def test_editor_and_admin_can_trigger_source_sync(
        self, project: AuthProject, sync_task: AsyncMock, role: Role
    ) -> None:
        """Pin: roles holding source.sync can still sync from the dashboard route."""
        resp = _post_as(_sync_client(project), project, role, _SYNC, json={})

        assert resp.status_code == 200
        sync_task.assert_called_once()

    def test_editor_can_trigger_source_sync(
        self, project: AuthProject, sync_task: AsyncMock
    ) -> None:
        resp = _post_as(_sync_client(project), project, Role.EDITOR, _SYNC, json={})

        assert resp.status_code == 200
        assert resp.json()["message"] == "Sync started for events"
        (event,) = _audit(project)
        assert event["event"] == AuditEvent.SYNC_TRIGGERED.value
        assert event["email"] == "editor@example.com"
        assert event["details"]["sources"] == ["events"]

    def test_viewer_cannot_trigger_source_sync(
        self, project: AuthProject, sync_task: AsyncMock
    ) -> None:
        resp = _post_as(_sync_client(project), project, Role.VIEWER, _SYNC, json={})

        assert resp.status_code == 403
        sync_task.assert_not_called()
        assert _audit(project) == []

    def test_api_key_viewer_cannot_trigger_source_sync(
        self, project: AuthProject, sync_task: AsyncMock
    ) -> None:
        user = project.add_user("viewer@example.com", Role.VIEWER)
        headers = {**_CSRF, "Authorization": f"Bearer {project.api_key(user)}"}

        resp = _sync_client(project).post(_SYNC, headers=headers, json={})

        assert resp.status_code == 403
        sync_task.assert_not_called()

    def test_anonymous_cannot_trigger_source_sync(
        self, project: AuthProject, sync_task: AsyncMock
    ) -> None:
        resp = _sync_client(project).post(_SYNC, headers=_CSRF, json={})

        assert resp.status_code == 401
        sync_task.assert_not_called()

    def test_auth_disabled_local_can_trigger_source_sync(
        self, project: AuthProject, sync_task: AsyncMock
    ) -> None:
        project.write_toggle("enabled: false\n")

        resp = _sync_client(project).post(_SYNC, headers=_CSRF, json={})

        assert resp.status_code == 200
        sync_task.assert_called_once()


@pytest.mark.unit
class TestInitialSyncPermissions:
    def test_status_viewer_ok(self, project: AuthProject, initial_state: InitialSyncState) -> None:
        user = project.add_user("viewer@example.com", Role.VIEWER)
        resp = _initial_client(project).get(
            _STATUS, headers={"Cookie": project.session_cookie(user)}
        )

        assert resp.status_code == 200
        assert resp.json()["phase"] == "syncing"

    def test_status_anonymous_rejected(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        assert _initial_client(project).get(_STATUS).status_code == 401

    @pytest.mark.parametrize("path", [_SKIP, _CANCEL])
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.VIEWER, 403),
            (Role.EDITOR, 403),
            (Role.ADMIN, 200),
        ],
    )
    def test_initial_sync_skip_and_cancel_require_admin(
        self,
        project: AuthProject,
        initial_state: InitialSyncState,
        path: str,
        role: Role,
        expected: int,
    ) -> None:
        resp = _post_as(_initial_client(project), project, role, path)

        assert resp.status_code == expected
        flag = (
            initial_state.skip_current_requested
            if path == _SKIP
            else initial_state.cancel_requested
        )
        assert flag is (expected == 200)
        assert len(_audit(project)) == (1 if expected == 200 else 0)

    def test_skip_and_cancel_audit_events(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        client = _initial_client(project)
        _post_as(client, project, Role.ADMIN, _SKIP)
        _post_as(client, project, Role.ADMIN, _CANCEL)

        events = _audit(project)
        assert [e["event"] for e in events] == [
            AuditEvent.INITIAL_SYNC_SKIPPED.value,
            AuditEvent.INITIAL_SYNC_CANCELLED.value,
        ]
        assert events[-1]["email"] == "admin@example.com"

    def test_skip_and_cancel_anonymous_rejected(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        client = _initial_client(project)

        assert client.post(_SKIP, headers=_CSRF).status_code == 401
        assert client.post(_CANCEL, headers=_CSRF).status_code == 401
        assert initial_state.skip_current_requested is False
        assert initial_state.cancel_requested is False

    def test_initial_sync_start_still_accepts_deploy_token(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        """Pin: /start keeps its own deploy-token check (public in the middleware)."""
        initial_state.phase = SyncPhase.IDLE
        token_dir = project.root / ".dango" / "state"
        token_dir.mkdir(parents=True, exist_ok=True)
        (token_dir / "deploy_token").write_text("test-token")
        source = MagicMock()
        source.name = "events"
        source.type.value = "csv"
        config = MagicMock()
        config.sources.sources = [source]

        with (
            patch("dango.config.helpers.load_config", return_value=config),
            patch("dango.web.routes.initial_sync._run_initial_sync", new_callable=AsyncMock),
        ):
            resp = _initial_client(project).post(
                _START, headers={**_CSRF, "Authorization": "Bearer test-token"}
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_initial_sync_start_without_credentials_rejected(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        initial_state.phase = SyncPhase.IDLE

        assert _initial_client(project).post(_START, headers=_CSRF).status_code == 401

    def test_auth_disabled_local_initial_sync_status_ok(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        project.write_toggle("enabled: false\n")

        resp = _initial_client(project).get(_STATUS)

        assert resp.status_code == 200

    def test_auth_disabled_local_skip_and_cancel_ok(
        self, project: AuthProject, initial_state: InitialSyncState
    ) -> None:
        project.write_toggle("enabled: false\n")
        client = _initial_client(project)

        assert client.post(_SKIP, headers=_CSRF).status_code == 200
        assert client.post(_CANCEL, headers=_CSRF).status_code == 200


@pytest.mark.unit
class TestDashboardBannerControls:
    def test_dashboard_exposes_role_and_gates_skip_cancel(self, project: AuthProject) -> None:
        from dango.web.routes.ui import router as ui_router

        user = project.add_user("viewer@example.com", Role.VIEWER)
        client = build_client(project.root, ui_router)

        resp = client.get("/", headers={"Cookie": project.session_cookie(user)})

        assert resp.status_code == 200
        assert 'window.DANGO_USER_ROLE = "viewer"' in resp.text
        assert "canManage && phase === 'syncing'" in resp.text
        assert "canManage && isRunning()" in resp.text
        assert "Requires Admin" in resp.text
