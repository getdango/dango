"""tests/unit/test_initial_sync.py

Unit tests for dango/web/routes/initial_sync.py.

Tests sync state machine, disk checks, API endpoints, deploy token,
and continue-on-failure behavior.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dango.web.routes.initial_sync import (
    InitialSyncState,
    SyncPhase,
    _check_disk_usage,
    _validate_deploy_token,
    router,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path):
    """Create a test FastAPI app with initial_sync router."""
    test_app = FastAPI()
    test_app.include_router(router)

    # Patch get_project_root to return tmp_path
    with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
        yield test_app


@pytest.fixture()
def client(app):
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level singleton before each test."""
    import dango.web.routes.initial_sync as mod

    mod._sync_state = InitialSyncState()
    yield
    mod._sync_state = InitialSyncState()


# ---------------------------------------------------------------------------
# 1. Sync state
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncState:
    def test_initial_idle(self):
        """State starts as IDLE."""
        state = InitialSyncState()
        assert state.phase == SyncPhase.IDLE
        assert state.total_sources == 0
        assert state.completed_sources == []

    def test_transitions(self):
        """State transitions update correctly."""
        state = InitialSyncState()
        state.phase = SyncPhase.SYNCING
        state.total_sources = 3
        state.current_source_index = 1
        state.current_source_name = "stripe"
        assert state.to_dict()["phase"] == "syncing"

    def test_json_persistence(self, tmp_path):
        """State can be serialized and loaded from JSON."""
        state = InitialSyncState(
            phase=SyncPhase.SYNCING,
            total_sources=3,
            current_source_index=2,
            current_source_name="stripe",
            completed_sources=["hubspot"],
        )

        state_file = tmp_path / ".dango" / "state" / "initial_sync.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(json.dumps(state.to_dict(), indent=2))

        # Verify round-trip
        data: dict = json.loads(state_file.read_text())
        assert data["phase"] == "syncing"
        assert data["current_source_name"] == "stripe"
        assert data["completed_sources"] == ["hubspot"]

    def test_restart_recovery(self, tmp_path):
        """Interrupted sync (SYNCING) is marked FAILED on reload."""
        state_file = tmp_path / ".dango" / "state" / "initial_sync.json"
        state_file.parent.mkdir(parents=True)
        state_file.write_text(
            json.dumps(
                {
                    "phase": "syncing",
                    "total_sources": 3,
                    "current_source_index": 1,
                    "current_source_name": "stripe",
                    "completed_sources": [],
                    "failed_sources": [],
                    "skipped_sources": [],
                    "cancel_requested": False,
                    "skip_current_requested": False,
                    "started_at": "2026-02-25T10:00:00",
                    "completed_at": "",
                    "error": None,
                }
            )
        )

        import dango.web.routes.initial_sync as mod

        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            mod._load_state()
            assert mod._sync_state.phase == SyncPhase.FAILED
            assert "interrupted" in (mod._sync_state.error or "").lower()


# ---------------------------------------------------------------------------
# 2. Disk check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiskCheck:
    @patch("dango.web.routes.initial_sync.shutil.disk_usage")
    def test_below_80_ok(self, mock_usage):
        """Disk below 80% returns None."""
        mock_usage.return_value = MagicMock(used=50, total=100)
        assert _check_disk_usage() is None

    @patch("dango.web.routes.initial_sync.shutil.disk_usage")
    def test_80_90_warns(self, mock_usage):
        """Disk 80-90% returns 'warn'."""
        mock_usage.return_value = MagicMock(used=85, total=100)
        assert _check_disk_usage() == "warn"

    @patch("dango.web.routes.initial_sync.shutil.disk_usage")
    def test_90_plus_aborts(self, mock_usage):
        """Disk >=90% returns 'abort'."""
        mock_usage.return_value = MagicMock(used=95, total=100)
        assert _check_disk_usage() == "abort"


# ---------------------------------------------------------------------------
# 3. API endpoints
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEndpoints:
    def test_status_returns_state(self, client, tmp_path):
        """GET /status returns current state."""
        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            resp = client.get("/api/initial-sync/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["phase"] == "idle"

    def test_start_when_already_running_409(self, client, tmp_path):
        """POST /start when syncing returns 409."""
        import dango.web.routes.initial_sync as mod

        mod._sync_state.phase = SyncPhase.SYNCING

        # Create token
        token_dir = tmp_path / ".dango" / "state"
        token_dir.mkdir(parents=True)
        (token_dir / "deploy_token").write_text("test-token")

        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/initial-sync/start",
                headers={"Authorization": "Bearer test-token"},
            )
        assert resp.status_code == 409

    @patch("dango.config.helpers.load_config")
    def test_start_with_deploy_token(self, mock_config, client, tmp_path):
        """POST /start with valid deploy token starts sync."""
        # Create token
        token_dir = tmp_path / ".dango" / "state"
        token_dir.mkdir(parents=True)
        (token_dir / "deploy_token").write_text("test-token")

        # Mock config
        mock_source = MagicMock()
        mock_source.name = "stripe"
        mock_source.type.value = "stripe"
        mock_cfg = MagicMock()
        mock_cfg.sources.sources = [mock_source]
        mock_config.return_value = mock_cfg

        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            resp = client.post(
                "/api/initial-sync/start",
                headers={"Authorization": "Bearer test-token"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_start_unauthorized(self, client, tmp_path):
        """POST /start without auth returns 401."""
        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            resp = client.post("/api/initial-sync/start")
        assert resp.status_code == 401

    def test_skip_source_not_syncing(self, client):
        """POST /skip-source when not syncing returns 409."""
        resp = client.post("/api/initial-sync/skip-source")
        assert resp.status_code == 409

    def test_cancel_not_syncing(self, client):
        """POST /cancel when not syncing returns 409."""
        resp = client.post("/api/initial-sync/cancel")
        assert resp.status_code == 409

    def test_skip_source_during_sync(self, client):
        """POST /skip-source during sync sets flag."""
        import dango.web.routes.initial_sync as mod

        mod._sync_state.phase = SyncPhase.SYNCING
        mod._sync_state.current_source_name = "stripe"

        resp = client.post("/api/initial-sync/skip-source")
        assert resp.status_code == 200
        assert mod._sync_state.skip_current_requested is True

    def test_cancel_during_sync(self, client):
        """POST /cancel during sync sets flag."""
        import dango.web.routes.initial_sync as mod

        mod._sync_state.phase = SyncPhase.SYNCING

        resp = client.post("/api/initial-sync/cancel")
        assert resp.status_code == 200
        assert mod._sync_state.cancel_requested is True


# ---------------------------------------------------------------------------
# 4. Deploy token
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployToken:
    def test_valid_token(self, tmp_path):
        """Valid token returns True and deletes the file."""
        token_dir = tmp_path / ".dango" / "state"
        token_dir.mkdir(parents=True)
        (token_dir / "deploy_token").write_text("my-secret-token")

        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            assert _validate_deploy_token("my-secret-token") is True
            # Token file deleted
            assert not (token_dir / "deploy_token").exists()

    def test_invalid_token(self, tmp_path):
        """Invalid token returns False."""
        token_dir = tmp_path / ".dango" / "state"
        token_dir.mkdir(parents=True)
        (token_dir / "deploy_token").write_text("correct-token")

        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            assert _validate_deploy_token("wrong-token") is False
            # Token file NOT deleted
            assert (token_dir / "deploy_token").exists()

    def test_no_token_file(self, tmp_path):
        """No token file returns False."""
        with patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path):
            assert _validate_deploy_token("any-token") is False

    def test_token_deleted_after_use(self, client, tmp_path):
        """Token is consumed on first successful use."""
        token_dir = tmp_path / ".dango" / "state"
        token_dir.mkdir(parents=True)
        (token_dir / "deploy_token").write_text("one-time-token")

        mock_source = MagicMock()
        mock_source.name = "test"
        mock_source.type.value = "test"
        mock_cfg = MagicMock()
        mock_cfg.sources.sources = [mock_source]

        with (
            patch("dango.web.routes.initial_sync.get_project_root", return_value=tmp_path),
            patch("dango.config.helpers.load_config", return_value=mock_cfg),
        ):
            # First use succeeds
            resp1 = client.post(
                "/api/initial-sync/start",
                headers={"Authorization": "Bearer one-time-token"},
            )
            assert resp1.status_code == 200

            # Reset state so we can try starting again
            import dango.web.routes.initial_sync as mod

            mod._sync_state = InitialSyncState()

            # Second use fails (token consumed)
            resp2 = client.post(
                "/api/initial-sync/start",
                headers={"Authorization": "Bearer one-time-token"},
            )
            assert resp2.status_code == 401


# ---------------------------------------------------------------------------
# 5. Continue on failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestContinueOnFailure:
    @patch("dango.web.routes.initial_sync._save_and_broadcast", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._sync_single_source", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._generate_dbt_docs")
    @patch("dango.web.routes.initial_sync._refresh_metabase")
    @pytest.mark.anyio
    async def test_one_fails_others_continue(
        self, mock_mb, mock_dbt, mock_sync, mock_broadcast, tmp_path
    ):
        """One source failure doesn't stop subsequent sources."""
        from dango.web.routes.initial_sync import _run_initial_sync

        mock_sync.side_effect = [
            None,  # source 1 succeeds
            RuntimeError("API error"),  # source 2 fails
            None,  # source 3 succeeds
        ]

        import dango.web.routes.initial_sync as mod

        mod._sync_state = InitialSyncState()

        with patch("dango.web.routes.initial_sync._check_disk_usage", return_value=None):
            await _run_initial_sync(
                tmp_path,
                [
                    {"name": "hubspot", "type": "hubspot"},
                    {"name": "stripe", "type": "stripe"},
                    {"name": "github", "type": "github"},
                ],
            )

        assert "hubspot" in mod._sync_state.completed_sources
        assert "github" in mod._sync_state.completed_sources
        assert len(mod._sync_state.failed_sources) == 1
        assert mod._sync_state.failed_sources[0]["name"] == "stripe"
        assert mod._sync_state.phase == SyncPhase.COMPLETE


# ---------------------------------------------------------------------------
# 6. Post-sync phases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPostSync:
    @patch("dango.web.routes.initial_sync._save_and_broadcast", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._sync_single_source", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._generate_dbt_docs")
    @patch("dango.web.routes.initial_sync._refresh_metabase")
    @pytest.mark.anyio
    async def test_dbt_docs_and_metabase_after_sync(
        self, mock_mb, mock_dbt, mock_sync, mock_broadcast, tmp_path
    ):
        """dbt docs + Metabase run after successful sync."""
        import dango.web.routes.initial_sync as mod

        mod._sync_state = InitialSyncState()

        with patch("dango.web.routes.initial_sync._check_disk_usage", return_value=None):
            await mod._run_initial_sync(
                tmp_path,
                [{"name": "hubspot", "type": "hubspot"}],
            )

        mock_dbt.assert_called_once_with(tmp_path)
        mock_mb.assert_called_once_with(tmp_path)

    @patch("dango.web.routes.initial_sync._save_and_broadcast", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._sync_single_source", new_callable=AsyncMock)
    @patch("dango.web.routes.initial_sync._generate_dbt_docs")
    @patch("dango.web.routes.initial_sync._refresh_metabase")
    @pytest.mark.anyio
    async def test_post_sync_skipped_on_all_failures(
        self, mock_mb, mock_dbt, mock_sync, mock_broadcast, tmp_path
    ):
        """Post-sync phases skipped if all sources failed."""
        import dango.web.routes.initial_sync as mod

        mod._sync_state = InitialSyncState()
        mock_sync.side_effect = RuntimeError("fail")

        with patch("dango.web.routes.initial_sync._check_disk_usage", return_value=None):
            await mod._run_initial_sync(
                tmp_path,
                [{"name": "hubspot", "type": "hubspot"}],
            )

        mock_dbt.assert_not_called()
        mock_mb.assert_not_called()
        assert mod._sync_state.phase == SyncPhase.COMPLETE
