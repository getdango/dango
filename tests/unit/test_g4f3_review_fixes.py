"""tests/unit/test_g4f3_review_fixes.py

Review fixes for the audit PR: 2FA disable accepts a recovery code, the CLI unlock clears
TOTP locks, and initial-sync, manual-sync and script-launch error text is redacted.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import dango.web.routes.initial_sync as initial_sync_mod
from dango.auth import database as db
from dango.auth.models import Role
from dango.auth.totp_guard import record_totp_failure, totp_lock_remaining
from dango.cli.main import cli
from dango.web.routes.initial_sync import InitialSyncState
from dango.web.routes.initial_sync import router as initial_sync_router
from tests.factories.auth_app_factories import AuthProject, build_client
from tests.unit.test_cli_auth import _add_user, _setup_project
from tests.unit.test_web_auth_2fa import _TEST_PASSWORD, _authed, _enable_2fa, _hdrs

_CANARY = "canary-review-secret-7e21b4"
_ERROR = f"401 Client Error for url https://api.example/v1?k={_CANARY}"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


@pytest.mark.unit
class TestDisableAcceptsRecoveryCode:
    def test_recovery_code_disables_and_is_consumed(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)  # recovery code AAAA-BBBB
        resp = client.post(
            "/api/auth/2fa/disable",
            json={"password": _TEST_PASSWORD, "code": "AAAA-BBBB"},
            headers=_hdrs(),
        )
        assert resp.status_code == 200, resp.text
        current = db.get_user_by_id(db_path, user.id)
        assert current is not None and current.totp_enabled is False

    def test_wrong_recovery_code_is_refused_and_counted(self, tmp_path: Path) -> None:
        client, db_path, user = _authed(tmp_path)
        _enable_2fa(db_path, user.id)
        statuses = [
            client.post(
                "/api/auth/2fa/disable",
                json={"password": _TEST_PASSWORD, "code": "ZZZZ-YYYY"},
                headers=_hdrs(),
            ).status_code
            for _ in range(5)
        ]
        assert statuses[:4] == [400] * 4 and statuses[4] == 423
        assert totp_lock_remaining(db_path, user.id) > 0
        current = db.get_user_by_id(db_path, user.id)
        assert current is not None and current.totp_enabled is True


@pytest.mark.unit
class TestCliUnlockClearsTotpLock:
    def test_unlock_clears_a_totp_only_lock(self, tmp_path: Path) -> None:
        project_root = _setup_project(tmp_path)
        db_path = project_root / ".dango" / "auth.db"
        user = _add_user(project_root, email="user@test.com")
        for _ in range(5):
            record_totp_failure(db_path, user.id, user.email)
        assert totp_lock_remaining(db_path, user.id) > 0

        with patch("dango.cli.utils.find_project_root", return_value=project_root):
            result = CliRunner().invoke(cli, ["auth", "unlock", "user@test.com"])

        assert result.exit_code == 0
        assert "not locked" not in result.output.lower()
        assert totp_lock_remaining(db_path, user.id) == 0


def _viewer_status(project: AuthProject) -> Any:
    viewer = project.add_user("viewer@example.com", Role.VIEWER)
    client = build_client(project.root, initial_sync_router)
    return client.get(
        "/api/initial-sync/status", headers={"Cookie": project.session_cookie(viewer)}
    )


@pytest.fixture
def project(tmp_path: Path) -> AuthProject:
    p = AuthProject(tmp_path)
    p.create_db()
    p.write_toggle("enabled: true\n")
    (tmp_path / ".env").write_text(f"SOURCE_API_VALUE={_CANARY}\n")
    return p


@pytest.fixture
def _initial_state(project: AuthProject) -> Any:
    initial_sync_mod._sync_state = InitialSyncState()
    with patch("dango.web.routes.initial_sync.get_project_root", return_value=project.root):
        yield
    initial_sync_mod._sync_state = InitialSyncState()


@pytest.mark.unit
@pytest.mark.usefixtures("_initial_state")
class TestInitialSyncStatusRedaction:
    def test_failed_source_error_is_redacted_for_a_viewer(self, project: AuthProject) -> None:
        with (
            patch.object(
                initial_sync_mod, "_sync_single_source", AsyncMock(side_effect=RuntimeError(_ERROR))
            ),
            patch.object(initial_sync_mod, "ws_manager", MagicMock(broadcast=AsyncMock())),
        ):
            asyncio.run(initial_sync_mod._run_initial_sync(project.root, [{"name": "stripe"}]))

        resp = _viewer_status(project)
        assert resp.status_code == 200
        assert _CANARY not in resp.text
        assert resp.json()["failed_sources"][0]["name"] == "stripe"
        state_file = project.root / ".dango" / "state" / "initial_sync.json"
        assert _CANARY not in state_file.read_text()

    def test_state_file_written_before_the_fix_is_redacted_when_loaded(
        self, project: AuthProject
    ) -> None:
        state_file = project.root / ".dango" / "state" / "initial_sync.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    "phase": "complete",
                    "failed_sources": [{"name": "stripe", "error": _ERROR}],
                }
            )
        )
        initial_sync_mod._load_state()
        assert _CANARY not in _viewer_status(project).text


@pytest.mark.unit
def test_manual_sync_failure_is_redacted_in_the_schedule_record(project: AuthProject) -> None:
    from dango.platform.scheduling.history import record_start
    from dango.web.routes.sync import _run_manual_sync
    from tests.unit.test_schedule_history_api import _create_db

    db_path = project.root / ".dango" / "scheduler.db"
    _create_db(project.root).rename(db_path)
    record_id = record_start(db_path, "manual", sources=["stripe"])
    with patch(
        "dango.platform.sync_process.launch_sync_subprocess", side_effect=RuntimeError(_ERROR)
    ):
        asyncio.run(_run_manual_sync(project.root, ["stripe"], False, None, record_id, db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        errors = [row[0] or "" for row in conn.execute("SELECT error FROM execution_history")]
    finally:
        conn.close()
    assert errors
    assert all(_CANARY not in e for e in errors)


@pytest.mark.unit
def test_script_launch_failure_response_is_redacted(project: AuthProject) -> None:
    from tests.unit.test_scripts_api import _make_app, _write_script

    _write_script(project.root / "scripts", "etl.py")
    app = _make_app(project.root)
    from starlette.testclient import TestClient

    with (
        patch("dango.web.routes.scripts_api.get_project_root", return_value=project.root),
        patch("dango.web.routes.scripts_api.run_as.require_isolation_ready"),
        patch(
            "dango.web.routes.scripts_api.script_launch.start_script", side_effect=OSError(_ERROR)
        ),
    ):
        resp = TestClient(app).post("/api/scripts/etl.py/run", headers=_hdrs())
    assert resp.status_code == 500
    assert _CANARY not in resp.text
