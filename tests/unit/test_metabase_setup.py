"""tests/unit/test_metabase_setup.py

Unit tests for setup_metabase() API interactions in
dango/visualization/metabase.py.

For MetabaseProvisioner and wait_for_metabase_ready tests, see
test_metabase_api.py. For sync_metabase_schema tests, see
test_metabase_schema_sync_core.py (core API interactions) and
test_metabase_schema_sync.py (re-sync poll, session reuse, URL resolution)
-- the former split out 1.0.8-AG to stay under the 500-line
file-size-check limit once the log-ready-check tests were added.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# setup_metabase API interactions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetupMetabaseApi:
    """Test setup_metabase API interactions (non-Docker paths)."""

    def test_already_configured_returns_early(self, tmp_path: Path) -> None:
        """When metabase.yml exists, return early without API calls."""
        from dango.visualization.metabase import setup_metabase

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text("url: http://localhost:3000")

        result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        assert "already configured" in result["errors"][0]

    def test_metabase_not_ready_returns_error(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import setup_metabase

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            # 1.0.8-AG: the log-ready check now runs first (real `docker logs`
            # subprocess polling, up to `ready_timeout` seconds) -- must be
            # mocked here too, or this test would take up to 200 real seconds
            # polling a nonexistent container before falling through to the
            # (mocked) health-check fallback.
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=False),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        assert "not ready" in result["errors"][0]

    def test_setup_metabase_uses_log_ready_check_first(self, tmp_path: Path) -> None:
        """1.0.8-AG: _wait_for_metabase_log_ready() is tried first. When it
        confirms readiness, wait_for_metabase_ready() (the /api/health
        fallback) must NOT be called at all -- `or` short-circuits and the
        log-based check is authoritative when it succeeds."""
        import requests

        from dango.visualization.metabase import setup_metabase

        mock_session = MagicMock(spec=requests.Session)
        # Fail fast at the very next step (setup token) -- irrelevant to what
        # this test verifies, just keeps it from doing unrelated real I/O.
        mock_session.get.return_value = MagicMock(status_code=500)

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            patch(
                "dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True
            ) as mock_log_ready,
            patch("dango.visualization.metabase.wait_for_metabase_ready") as mock_health_check,
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        mock_log_ready.assert_called_once()
        mock_health_check.assert_not_called()
        # Progressed past the readiness gate entirely (next failure is the
        # setup-token step, not "not ready").
        assert "not ready" not in result["errors"][0]

    def test_setup_metabase_falls_back_to_health_check(self, tmp_path: Path) -> None:
        """1.0.8-AG: when the log-ready check doesn't confirm readiness (e.g. a
        Metabase version that logs the line differently, or a docker-logs
        failure), setup_metabase() falls back to the original /api/health
        poll, same budget -- confirms the fallback path still works and is
        actually reached."""
        import requests

        from dango.visualization.metabase import setup_metabase

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = MagicMock(status_code=500)

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            patch(
                "dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False
            ) as mock_log_ready,
            patch(
                "dango.visualization.metabase.wait_for_metabase_ready", return_value=True
            ) as mock_health_check,
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        mock_log_ready.assert_called_once()
        mock_health_check.assert_called_once()
        # Readiness itself succeeded via the fallback -- next failure is the
        # setup-token step, not "not ready".
        assert "not ready" not in result["errors"][0]

    def test_setup_metabase_fails_when_both_checks_fail(self, tmp_path: Path) -> None:
        """1.0.8-AG: when both the log-ready check and the health-check
        fallback fail, setup_metabase() reports failure the same way it
        always has -- no regression in the failure-reporting path."""
        from dango.visualization.metabase import setup_metabase

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=False),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert result["success"] is False
        assert any("not ready" in e for e in result["errors"])

    def test_cannot_get_setup_token_returns_error(self, tmp_path: Path) -> None:
        import requests

        from dango.visualization.metabase import setup_metabase

        mock_session = MagicMock(spec=requests.Session)
        props_resp = MagicMock()
        props_resp.status_code = 500
        mock_session.get.return_value = props_resp

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            # 1.0.8-AG: log-ready check runs first; force it False so these
            # pre-existing tests fall straight through to the (mocked) health
            # check instead of real-polling a nonexistent container for up to
            # `ready_timeout` seconds.
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        assert "setup token" in result["errors"][0]

    def test_fresh_metabase_setup_creates_admin_and_duckdb(self, tmp_path: Path) -> None:
        """Happy path: fresh Metabase with setup token -> create admin -> connect DuckDB."""
        import requests

        from dango.visualization.metabase import setup_metabase

        mock_session = MagicMock(spec=requests.Session)

        # session/properties -> has setup token
        props_resp = MagicMock(status_code=200)
        props_resp.json.return_value = {"setup-token": "tok-abc"}
        # POST /api/setup -> success
        setup_resp = MagicMock(status_code=200)
        # POST /api/session (login after setup) -> success
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-xyz"}
        # GET /api/database (check existing) -> empty list
        db_list_resp = MagicMock(status_code=200)
        db_list_resp.json.return_value = {"data": []}
        # POST /api/database (create DuckDB) -> success
        create_db_resp = MagicMock(status_code=200)
        create_db_resp.json.return_value = {"id": 7}
        # PUT /api/database/{id} (set default) -> success
        set_default_resp = MagicMock(status_code=200)
        # GET /api/database (H2 deletion) -> no H2
        h2_list_resp = MagicMock(status_code=200)
        h2_list_resp.json.return_value = {"data": []}
        # GET /api/collection -> empty
        coll_list_resp = MagicMock(status_code=200)
        coll_list_resp.json.return_value = []
        # POST /api/collection (Shared, Personal) -> success
        coll_create_resp = MagicMock(status_code=200)

        # Order of GET calls: session/properties, db list, H2 list, collection list
        mock_session.get.side_effect = [
            props_resp,
            db_list_resp,
            h2_list_resp,
            coll_list_resp,
        ]
        # Order of POST: /api/setup, /api/session, /api/database (create),
        #   /api/collection (Shared), /api/collection (Personal)
        mock_session.post.side_effect = [
            setup_resp,
            login_resp,
            create_db_resp,
            coll_create_resp,
            coll_create_resp,
        ]
        mock_session.put.side_effect = [set_default_resp]

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            # 1.0.8-AG: log-ready check runs first; force it False so these
            # pre-existing tests fall straight through to the (mocked) health
            # check instead of real-polling a nonexistent container for up to
            # `ready_timeout` seconds.
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert result["success"] is True
        assert result["admin_created"] is True
        assert result["duckdb_connected"] is True
        assert result["duckdb_id"] == 7
        assert result["credentials_saved"] is True

        # Verify credentials file was written
        creds_file = tmp_path / ".dango" / "metabase.yml"
        assert creds_file.exists()

    def test_duckdb_connection_failure_does_not_save_credentials(self, tmp_path: Path) -> None:
        """When DuckDB creation fails, don't save credentials - allow retry."""
        import requests

        from dango.visualization.metabase import setup_metabase

        mock_session = MagicMock(spec=requests.Session)

        # session/properties -> has setup token
        props_resp = MagicMock(status_code=200)
        props_resp.json.return_value = {"setup-token": "tok-abc"}
        # POST /api/setup -> success
        setup_resp = MagicMock(status_code=200)
        # POST /api/session (login after setup) -> success
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-xyz"}
        # GET /api/database (check existing) -> empty
        db_list_resp = MagicMock(status_code=200)
        db_list_resp.json.return_value = {"data": []}
        # POST /api/database (create DuckDB) -> FAILS
        create_db_resp = MagicMock(status_code=500)
        create_db_resp.text = "Internal server error"

        # Order: session/properties, database list (existing)
        mock_session.get.side_effect = [
            props_resp,
            db_list_resp,
        ]
        # Order: /api/setup, /api/session, /api/database (create - fails)
        mock_session.post.side_effect = [
            setup_resp,
            login_resp,
            create_db_resp,
        ]

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
            # 1.0.8-AG: log-ready check runs first; force it False so these
            # pre-existing tests fall straight through to the (mocked) health
            # check instead of real-polling a nonexistent container for up to
            # `ready_timeout` seconds.
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        assert not result["duckdb_connected"]
        # Should NOT have saved credentials
        assert not result["credentials_saved"]
        creds_file = tmp_path / ".dango" / "metabase.yml"
        assert not creds_file.exists()
