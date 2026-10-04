"""tests/unit/test_dashboard_manager_credentials.py

Credential-boundary tests prevent direct Metabase password reads from dashboard workflows.
"""

from unittest.mock import patch

import pytest

from dango.security import MetabaseCredentialStoreError
from dango.visualization.dashboard_manager import DashboardManager


@patch("dango.visualization.dashboard_manager.requests.post")
@patch("dango.visualization.dashboard_manager.load_metabase_admin_credentials")
def test_manager_uses_security_credential_boundary(mock_credentials, mock_post, tmp_path):
    """Boundary credentials become the unchanged Metabase session payload."""
    mock_credentials.return_value = ("admin@example.com", "protected-password")
    mock_post.return_value.status_code = 200
    mock_post.return_value.json.return_value = {"id": "session-token"}

    manager = DashboardManager(tmp_path, metabase_url="http://metabase.test")

    mock_credentials.assert_called_once_with(tmp_path)
    mock_post.assert_called_once_with(
        "http://metabase.test/api/session",
        json={"username": "admin@example.com", "password": "protected-password"},
        timeout=10,
    )
    assert manager.session_token == "session-token"


@patch("dango.visualization.dashboard_manager.requests.post")
@patch("dango.visualization.dashboard_manager.load_metabase_admin_credentials", return_value=None)
def test_manager_does_not_post_without_credentials(mock_credentials, mock_post, tmp_path):
    """Absent credentials leave the manager unauthenticated without a request."""
    manager = DashboardManager(tmp_path)

    mock_credentials.assert_called_once_with(tmp_path)
    mock_post.assert_not_called()
    assert manager.session_token is None


@patch(
    "dango.visualization.dashboard_manager.load_metabase_admin_credentials",
    side_effect=MetabaseCredentialStoreError("Protected credential store is malformed."),
)
@patch("dango.visualization.dashboard_manager.requests.post")
def test_manager_propagates_malformed_protected_secret(mock_post, _mock_credentials, tmp_path):
    """Malformed protected state remains visible instead of reading legacy YAML."""
    with pytest.raises(MetabaseCredentialStoreError, match="malformed"):
        DashboardManager(tmp_path)

    mock_post.assert_not_called()
