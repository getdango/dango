"""tests/unit/test_metabase_setup.py

Unit tests for sync_metabase_schema and setup_metabase API interactions
in dango/visualization/metabase.py.

For MetabaseProvisioner and wait_for_metabase_ready tests,
see test_metabase_api.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sync_metabase_schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncMetabaseSchema:
    """Test sync_metabase_schema API interactions."""

    def test_returns_false_when_credentials_file_missing(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import sync_metabase_schema

        result = sync_metabase_schema(tmp_path)
        assert result is False

    def test_successful_sync_returns_true(self, tmp_path: Path) -> None:
        """Full success path: login, sync_schema, poll, update tables."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        # Create credentials file
        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)

        # Login response
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"id": "sess-abc"}

        # Sync schema response
        sync_resp = MagicMock()
        sync_resp.status_code = 200

        # Poll response — sync complete
        poll_resp = MagicMock()
        poll_resp.status_code = 200
        poll_resp.json.return_value = {"initial_sync_status": "complete"}

        # Metadata response
        metadata_resp = MagicMock()
        metadata_resp.status_code = 200
        metadata_resp.json.return_value = {
            "tables": [
                {"id": 1, "name": "stg_orders", "schema": "staging"},
                {"id": 2, "name": "_dlt_loads", "schema": "raw_sales"},
            ]
        }

        # Table update response
        update_resp = MagicMock()
        update_resp.status_code = 200

        mock_session.post.side_effect = [login_resp, sync_resp]
        mock_session.get.side_effect = [poll_resp, metadata_resp]
        mock_session.put.return_value = update_resp

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            result = sync_metabase_schema(tmp_path)

        assert result is True
        # Verify login call
        assert mock_session.post.call_args_list[0][0][0] == "http://localhost:3000/api/session"
        # Verify sync_schema call
        assert (
            mock_session.post.call_args_list[1][0][0]
            == "http://localhost:3000/api/database/5/sync_schema"
        )
        # Verify table updates — both tables should be updated
        assert mock_session.put.call_count == 2

    def test_returns_false_when_login_fails(self, tmp_path: Path) -> None:
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "admin": {"email": "admin@test.com", "password": "wrong"},
                    "database": {"id": 5},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock()
        login_resp.status_code = 401
        mock_session.post.return_value = login_resp

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            result = sync_metabase_schema(tmp_path)

        assert result is False

    def test_returns_false_when_database_id_missing(self, tmp_path: Path) -> None:
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {},  # No ID
                }
            )
        )

        result = sync_metabase_schema(tmp_path)
        assert result is False

    def test_returns_false_when_sync_schema_fails(self, tmp_path: Path) -> None:
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"id": "sess-abc"}

        sync_resp = MagicMock()
        sync_resp.status_code = 500

        mock_session.post.side_effect = [login_resp, sync_resp]

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            result = sync_metabase_schema(tmp_path)

        assert result is False

    def test_returns_false_when_no_tables_found(self, tmp_path: Path) -> None:
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock()
        login_resp.status_code = 200
        login_resp.json.return_value = {"id": "sess-abc"}

        sync_resp = MagicMock()
        sync_resp.status_code = 200

        poll_resp = MagicMock()
        poll_resp.status_code = 200
        poll_resp.json.return_value = {"initial_sync_status": "complete"}

        metadata_resp = MagicMock()
        metadata_resp.status_code = 200
        metadata_resp.json.return_value = {"tables": []}  # No tables

        mock_session.post.side_effect = [login_resp, sync_resp]
        mock_session.get.side_effect = [poll_resp, metadata_resp]

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            result = sync_metabase_schema(tmp_path)

        assert result is False

    def test_hides_staging_and_raw_tables(self, tmp_path: Path) -> None:
        """Verify tables in _staging/raw/raw_* schemas get visibility_type='hidden'."""
        import requests
        import yaml

        from dango.visualization.metabase import sync_metabase_schema

        creds_dir = tmp_path / ".dango"
        creds_dir.mkdir()
        creds_file = creds_dir / "metabase.yml"
        creds_file.write_text(
            yaml.dump(
                {
                    "admin": {"email": "admin@test.com", "password": "secret"},
                    "database": {"id": 5},
                }
            )
        )

        mock_session = MagicMock(spec=requests.Session)
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-abc"}
        sync_resp = MagicMock(status_code=200)
        poll_resp = MagicMock(status_code=200)
        poll_resp.json.return_value = {"initial_sync_status": "complete"}
        metadata_resp = MagicMock(status_code=200)
        metadata_resp.json.return_value = {
            "tables": [
                {"id": 1, "name": "my_table", "schema": "raw_stripe_staging"},
                {"id": 2, "name": "_dlt_loads", "schema": "raw"},
                {"id": 3, "name": "my_model", "schema": "staging"},
            ]
        }
        update_resp = MagicMock(status_code=200)

        mock_session.post.side_effect = [login_resp, sync_resp]
        mock_session.get.side_effect = [poll_resp, metadata_resp]
        mock_session.put.return_value = update_resp

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            result = sync_metabase_schema(tmp_path)

        assert result is True

        # Check put calls for visibility_type
        hidden_calls = [
            c
            for c in mock_session.put.call_args_list
            if c[1]["json"].get("visibility_type") == "hidden"
        ]
        assert len(hidden_calls) == 2  # raw_stripe_staging + raw

        # Check staging table does NOT have visibility_type=hidden
        staging_calls = [
            c
            for c in mock_session.put.call_args_list
            if c[1]["json"].get("description", "").startswith("\u2705")
        ]
        assert len(staging_calls) == 1
        assert "visibility_type" not in staging_calls[0][1]["json"]


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
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=False),
        ):
            result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        assert "not ready" in result["errors"][0]

    def test_cannot_get_setup_token_returns_error(self, tmp_path: Path) -> None:
        import requests

        from dango.visualization.metabase import setup_metabase

        mock_session = MagicMock(spec=requests.Session)
        props_resp = MagicMock()
        props_resp.status_code = 500
        mock_session.get.return_value = props_resp

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-abc"),
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
