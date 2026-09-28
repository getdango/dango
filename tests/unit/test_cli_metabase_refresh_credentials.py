"""tests/unit/test_cli_metabase_refresh_credentials.py

Tests for protected credentials in the Metabase refresh CLI command.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.security import MetabaseCredentialStoreError


@pytest.fixture
def metadata() -> dict[str, object]:
    return {
        "metabase_url": "http://localhost:3000",
        "database": {"id": 42},
        "admin": {"email": "admin@example.com"},
    }


def _invoke_refresh(project_root: Path):
    from dango.cli.commands.metabase_cmd import metabase_refresh

    return CliRunner().invoke(metabase_refresh, obj={"project_root": project_root})


@pytest.mark.unit
def test_refresh_uses_protected_credentials_for_schema_display(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    session_response = MagicMock(status_code=200)
    session_response.json.return_value = {"id": "display-session"}
    health_response = MagicMock(status_code=200)
    metadata_response = MagicMock(status_code=200)
    metadata_response.json.return_value = {"tables": []}

    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch(
            "dango.cli.commands.metabase_cmd.load_metabase_metadata",
            return_value=metadata,
        ),
        patch(
            "dango.cli.commands.metabase_cmd.load_metabase_admin_credentials",
            return_value=("admin@example.com", "protected-password"),
        ),
        patch("requests.get", side_effect=[health_response, metadata_response]),
        patch("requests.post", return_value=session_response) as post,
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(True, None, "refresh-session"),
        ),
        patch("dango.visualization.metabase.sync_metabase_schema", return_value=True),
    ):
        result = _invoke_refresh(tmp_path)

    assert result.exit_code == 0, result.output
    post.assert_called_once_with(
        "http://localhost:3000/api/session",
        json={"username": "admin@example.com", "password": "protected-password"},
        timeout=10,
    )


@pytest.mark.unit
def test_refresh_skips_schema_display_without_admin_credential(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    health_response = MagicMock(status_code=200)

    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch(
            "dango.cli.commands.metabase_cmd.load_metabase_metadata",
            return_value=metadata,
        ),
        patch(
            "dango.cli.commands.metabase_cmd.load_metabase_admin_credentials",
            return_value=None,
        ),
        patch("requests.get", return_value=health_response),
        patch("requests.post") as post,
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(True, None, "refresh-session"),
        ),
        patch("dango.visualization.metabase.sync_metabase_schema", return_value=True),
    ):
        result = _invoke_refresh(tmp_path)

    assert result.exit_code == 0, result.output
    assert "credentials are unavailable" in result.output
    post.assert_not_called()


@pytest.mark.unit
def test_refresh_does_not_hide_malformed_protected_credential(
    tmp_path: Path, metadata: dict[str, object]
) -> None:
    health_response = MagicMock(status_code=200)

    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch(
            "dango.cli.commands.metabase_cmd.load_metabase_metadata",
            return_value=metadata,
        ),
        patch(
            "dango.cli.commands.metabase_cmd.load_metabase_admin_credentials",
            side_effect=MetabaseCredentialStoreError("protected credential is malformed"),
        ),
        patch("requests.get", return_value=health_response),
        patch("requests.post") as post,
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(True, None, "refresh-session"),
        ),
        patch("dango.visualization.metabase.sync_metabase_schema", return_value=True),
    ):
        result = _invoke_refresh(tmp_path)

    assert result.exit_code != 0
    assert "protected credential is malformed" in result.output
    post.assert_not_called()
