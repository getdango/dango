"""tests/unit/test_metabase_sync_credentials.py

Tests for the auth Metabase administrator-credential access boundary.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.security import MetabaseCredentialStoreError

MB_URL = "http://localhost:3000"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Create a project containing legacy Metabase and project metadata."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "project.yml").write_text(
        yaml.safe_dump({"project": {"id": "metabase-sync-credentials-test"}}),
        encoding="utf-8",
    )
    (dango_dir / "metabase.yml").write_text(
        yaml.safe_dump(
            {
                "metabase_url": MB_URL,
                "admin": {"email": "admin@example.com", "password": "legacy-password"},
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.unit
class TestMetabaseSyncCredentials:
    """The auth sync session uses the S2a credential boundary exclusively."""

    @patch("dango.auth.metabase_sync.requests.post")
    @patch("dango.auth.metabase_sync.load_metabase_admin_credentials")
    def test_admin_session_uses_access_boundary(
        self,
        load_credentials: MagicMock,
        post: MagicMock,
        project_root: Path,
    ) -> None:
        """Boundary credentials are sent unchanged to the Metabase session API."""
        from dango.auth.metabase_sync import _get_admin_session

        load_credentials.return_value = ("protected@example.com", "protected-password")
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"id": "admin-session"}

        assert _get_admin_session(MB_URL, project_root) == "admin-session"
        post.assert_called_once_with(
            f"{MB_URL}/api/session",
            json={"username": "protected@example.com", "password": "protected-password"},
            timeout=10,
        )

    @patch("dango.auth.metabase_sync.requests.post")
    @patch("dango.auth.metabase_sync.load_metabase_admin_credentials", return_value=None)
    def test_admin_session_returns_none_when_access_boundary_has_no_credential(
        self,
        load_credentials: MagicMock,
        post: MagicMock,
        project_root: Path,
    ) -> None:
        """Missing boundary credentials do not issue a Metabase API request."""
        from dango.auth.metabase_sync import _get_admin_session

        assert _get_admin_session(MB_URL, project_root) is None
        load_credentials.assert_called_once_with(project_root)
        post.assert_not_called()

    @patch("dango.auth.metabase_sync.requests.post")
    @patch(
        "dango.auth.metabase_sync.load_metabase_admin_credentials",
        side_effect=MetabaseCredentialStoreError("protected secret is malformed"),
    )
    def test_admin_session_propagates_malformed_protected_secret(
        self,
        load_credentials: MagicMock,
        post: MagicMock,
        project_root: Path,
    ) -> None:
        """A bad protected secret is never hidden by a direct YAML fallback."""
        from dango.auth.metabase_sync import _get_admin_session

        with pytest.raises(MetabaseCredentialStoreError, match="malformed"):
            _get_admin_session(MB_URL, project_root)

        load_credentials.assert_called_once_with(project_root)
        post.assert_not_called()
