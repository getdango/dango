"""tests/unit/test_metabase_schema_sync_core.py

Unit tests for sync_metabase_schema()'s core API interactions (login,
schema-sync trigger, table visibility updates) in
dango/visualization/metabase.py.

Split out of test_metabase_setup.py (1.0.8-AG) -- that file's
setup_metabase() coverage grew past the 500-line file-size-check limit
once the log-ready-check tests were added; this class had no dependency
on setup_metabase() and moved cleanly. Named distinctly from the
similarly-scoped test_metabase_schema_sync.py (which covers the re-sync
completion poll, session reuse, and URL resolution edge cases) to avoid
the two files being confused for each other.

For MetabaseProvisioner and wait_for_metabase_ready tests,
see test_metabase_api.py. For setup_metabase() tests, see
test_metabase_setup.py.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# sync_metabase_schema
# ---------------------------------------------------------------------------


def _empty_task_resp() -> MagicMock:
    """Mock GET /api/task response with no prior tasks (used as poll baseline)."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": []}
    return resp


def _task_resp(status: str, task_id: int = 1, db_id: int = 5) -> MagicMock:
    """Mock GET /api/task response containing one "sync" task with the given status."""
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "data": [{"id": task_id, "task": "sync", "status": status, "db_id": db_id}]
    }
    return resp


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
        # Baseline task lookup (no prior tasks), then the triggered "sync" task
        # already showing as finished.
        mock_session.get.side_effect = [_empty_task_resp(), _task_resp("success"), metadata_resp]
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

        metadata_resp = MagicMock()
        metadata_resp.status_code = 200
        metadata_resp.json.return_value = {"tables": []}  # No tables

        mock_session.post.side_effect = [login_resp, sync_resp]
        mock_session.get.side_effect = [_empty_task_resp(), _task_resp("success"), metadata_resp]

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
        mock_session.get.side_effect = [_empty_task_resp(), _task_resp("success"), metadata_resp]
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
            if c[1]["json"].get("description", "").startswith("✅")
        ]
        assert len(staging_calls) == 1
        assert "visibility_type" not in staging_calls[0][1]["json"]
