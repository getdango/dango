"""tests/unit/test_metabase_api.py

Unit tests for Metabase API-calling functions in dango/visualization/metabase.py.

Covers: wait_for_metabase_ready and MetabaseProvisioner methods.
For sync_metabase_schema and setup_metabase tests, see test_metabase_setup.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# wait_for_metabase_ready
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWaitForMetabaseReady:
    """Test wait_for_metabase_ready polling logic."""

    @patch("dango.visualization.metabase.time.sleep")
    def test_returns_true_when_health_responds_200(self, mock_sleep: MagicMock) -> None:
        import requests

        from dango.visualization.metabase import wait_for_metabase_ready

        mock_session = MagicMock(spec=requests.Session)
        health_response = MagicMock()
        health_response.status_code = 200
        mock_session.get.return_value = health_response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            result = wait_for_metabase_ready("http://localhost:3000", timeout=10)

        assert result is True
        mock_session.get.assert_called_with("http://localhost:3000/api/health", timeout=5)

    @patch("dango.visualization.metabase.time.sleep")
    def test_returns_false_on_timeout(self, mock_sleep: MagicMock) -> None:
        import requests

        from dango.visualization.metabase import wait_for_metabase_ready

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value.status_code = 503

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            # Patch time.time to simulate timeout
            with patch("dango.visualization.metabase.time.time") as mock_time:
                mock_time.side_effect = [0, 20]  # start=0, next check=20 -> elapsed=20 > timeout
                result = wait_for_metabase_ready("http://localhost:3000", timeout=10)

        assert result is False

    @patch("dango.visualization.metabase.time.sleep")
    def test_retries_on_exception(self, mock_sleep: MagicMock) -> None:
        import requests

        from dango.visualization.metabase import wait_for_metabase_ready

        mock_session = MagicMock(spec=requests.Session)
        # First call raises, second succeeds
        success_response = MagicMock()
        success_response.status_code = 200
        mock_session.get.side_effect = [
            requests.ConnectionError("refused"),
            success_response,
        ]

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            with patch("dango.visualization.metabase.time.time") as mock_time:
                mock_time.side_effect = [0, 0, 5]  # succeed on second check
                result = wait_for_metabase_ready(timeout=60)

        assert result is True
        assert mock_session.get.call_count == 2


# ---------------------------------------------------------------------------
# MetabaseProvisioner.authenticate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetabaseProvisionerAuthenticate:
    """Test MetabaseProvisioner.authenticate."""

    def test_success_sets_session_token(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"id": "abc-session-token"}
        mock_session.post.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner(
                metabase_url="http://localhost:3000",
                username="admin@test.com",
                password="secret",
            )

        result = provisioner.authenticate()

        assert result is True
        assert provisioner.session_token == "abc-session-token"
        mock_session.post.assert_called_once_with(
            "http://localhost:3000/api/session",
            json={"username": "admin@test.com", "password": "secret"},
            timeout=10,
        )

    def test_returns_false_on_401(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 401
        mock_session.post.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        result = provisioner.authenticate()

        assert result is False
        assert provisioner.session_token is None

    def test_returns_false_on_connection_error(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.side_effect = requests.ConnectionError("refused")

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        result = provisioner.authenticate()

        assert result is False


# ---------------------------------------------------------------------------
# MetabaseProvisioner.get_database_id
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetabaseProvisionerGetDatabaseId:
    """Test MetabaseProvisioner.get_database_id."""

    def test_returns_none_without_session_token(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.session_token = None

        result = provisioner.get_database_id()

        assert result is None

    def test_finds_duckdb_by_name(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "data": [
                {"id": 1, "name": "H2 Sample", "engine": "h2"},
                {"id": 2, "name": "DuckDB Analytics", "engine": "duckdb"},
                {"id": 3, "name": "Postgres Prod", "engine": "postgres"},
            ]
        }
        mock_session.get.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.get_database_id("DuckDB")

        assert result == 2

    def test_returns_none_when_no_match(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"data": []}
        mock_session.get.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.get_database_id("DuckDB")

        assert result is None

    def test_returns_none_on_api_error(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.side_effect = requests.ConnectionError("timeout")

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.get_database_id()

        assert result is None


# ---------------------------------------------------------------------------
# MetabaseProvisioner.create_card / create_dashboard / add_card_to_dashboard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMetabaseProvisionerCreateCard:
    """Test MetabaseProvisioner.create_card."""

    def test_success_returns_card_id(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"id": 42}
        mock_session.post.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.create_card("pipeline_health_score", database_id=5)

        assert result == 42
        call_args = mock_session.post.call_args
        assert call_args[0][0] == "http://localhost:3000/api/card"
        assert call_args[1]["json"]["name"] == "Pipeline Health Score"
        assert call_args[1]["json"]["dataset_query"]["database"] == 5

    def test_returns_none_without_session_token(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.session_token = None

        result = provisioner.create_card("pipeline_health_score", database_id=5)
        assert result is None

    def test_returns_none_for_unknown_query_key(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.session_token = "tok-123"

        result = provisioner.create_card("nonexistent_key", database_id=5)
        assert result is None

    def test_returns_none_on_api_failure(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 500
        mock_session.post.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.create_card("pipeline_health_score", database_id=5)
        assert result is None


@pytest.mark.unit
class TestMetabaseProvisionerCreateDashboard:
    """Test MetabaseProvisioner.create_dashboard."""

    def test_success_returns_dashboard_id(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"id": 10}
        mock_session.post.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.create_dashboard("My Dashboard", "A test dashboard")

        assert result == 10
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        assert call_args[0][0] == "http://localhost:3000/api/dashboard"
        assert call_args[1]["json"]["name"] == "My Dashboard"

    def test_returns_none_without_session_token(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.session_token = None

        result = provisioner.create_dashboard()
        assert result is None


@pytest.mark.unit
class TestMetabaseProvisionerAddCardToDashboard:
    """Test MetabaseProvisioner.add_card_to_dashboard."""

    def test_success_returns_true(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        response = MagicMock()
        response.status_code = 200
        mock_session.post.return_value = response

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.add_card_to_dashboard(
            dashboard_id=10, card_id=42, row=0, col=0, size_x=6, size_y=4
        )

        assert result is True
        call_args = mock_session.post.call_args
        assert call_args[0][0] == "http://localhost:3000/api/dashboard/10/cards"
        assert call_args[1]["json"]["cardId"] == 42
        assert call_args[1]["json"]["row"] == 0

    def test_returns_false_without_session_token(self) -> None:
        from dango.visualization.metabase import MetabaseProvisioner

        provisioner = MetabaseProvisioner()
        provisioner.session_token = None

        result = provisioner.add_card_to_dashboard(10, 42)
        assert result is False

    def test_returns_false_on_api_error(self) -> None:
        import requests

        from dango.visualization.metabase import MetabaseProvisioner

        mock_session = MagicMock(spec=requests.Session)
        mock_session.post.side_effect = requests.ConnectionError("refused")

        with patch("dango.visualization.metabase.requests.Session", return_value=mock_session):
            provisioner = MetabaseProvisioner()

        provisioner.session_token = "tok-123"
        result = provisioner.add_card_to_dashboard(10, 42)
        assert result is False
