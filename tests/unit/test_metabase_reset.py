"""tests/unit/test_metabase_reset.py

Tests for Metabase setup error messages containing correct volume names.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestSetupMetabaseErrorMessages:
    """Verify error messages include the correct Docker volume name."""

    def test_reset_failed_error_includes_compose_name(self, tmp_path: Path) -> None:
        """When _reset_metabase_volume returns False, error includes compose name."""
        credentials_file = tmp_path / ".dango" / "metabase.yml"
        # Ensure credentials file does NOT exist (triggers setup flow)
        assert not credentials_file.exists()

        with (
            patch(
                "dango.platform.docker.get_compose_project_name",
                return_value="dango-abc123",
            ),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
            patch("dango.visualization.metabase._reset_metabase_volume", return_value=False),
        ):
            import requests

            mock_session = MagicMock(spec=requests.Session)
            # First call: session properties — no setup token
            props_response = MagicMock()
            props_response.status_code = 200
            props_response.json.return_value = {"setup-token": None}

            # Second call: login attempt — fails (stale volume)
            login_response = MagicMock()
            login_response.status_code = 401

            mock_session.get.return_value = props_response
            mock_session.post.return_value = login_response

            with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
                from dango.visualization.metabase import setup_metabase

                result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        error_msg = result["errors"][0]
        assert "dango-abc123_metabase-data" in error_msg
        assert "docker volume rm" in error_msg

    def test_setup_api_failure_error_includes_compose_name(self, tmp_path: Path) -> None:
        """When setup API returns non-200 and login fails, error includes compose name."""
        with (
            patch(
                "dango.platform.docker.get_compose_project_name",
                return_value="dango-xyz789",
            ),
            patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
        ):
            import requests

            mock_session = MagicMock(spec=requests.Session)

            # session properties — has setup token (fresh Metabase)
            props_response = MagicMock()
            props_response.status_code = 200
            props_response.json.return_value = {"setup-token": "tok-123"}

            # setup POST — fails with "user exists"
            setup_response = MagicMock()
            setup_response.status_code = 400
            setup_response.text = "first user already exists"

            # login attempt — also fails
            login_response = MagicMock()
            login_response.status_code = 401

            mock_session.get.return_value = props_response
            mock_session.post.side_effect = [setup_response, login_response]

            with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
                from dango.visualization.metabase import setup_metabase

                result = setup_metabase(tmp_path, "test-project", "admin@example.com")

        assert not result["success"]
        error_msg = result["errors"][0]
        assert "dango-xyz789_metabase-data" in error_msg
        assert "docker volume rm" in error_msg
