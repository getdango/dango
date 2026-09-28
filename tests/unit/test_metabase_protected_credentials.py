"""tests/unit/test_metabase_protected_credentials.py

Focused coverage for protected Metabase credentials in visualization readers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest


def _write_metadata(project_root: Path, **metadata: object) -> None:
    dango_dir = project_root / ".dango"
    dango_dir.mkdir()
    (dango_dir / "metabase.yml").write_text(
        "metabase_url: http://metabase.test\n"
        "database:\n"
        "  id: 5\n"
        "admin:\n"
        "  email: admin@example.com\n"
        + "".join(f"{key}: {value}\n" for key, value in metadata.items()),
        encoding="utf-8",
    )


@pytest.mark.unit
class TestProtectedCredentialsForVisualizationReaders:
    def test_site_url_catchup_uses_protected_credential(self, tmp_path: Path) -> None:
        """A catch-up login reads the boundary, not YAML admin.password."""
        from dango.visualization.metabase import _apply_metabase_site_url_catchup

        _write_metadata(tmp_path)
        session = MagicMock()
        login_response = MagicMock(status_code=200)
        login_response.json.return_value = {"id": "protected-session"}
        session.post.return_value = login_response
        session.put.return_value = MagicMock(ok=True)

        with (
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials",
                return_value=("admin@example.com", "protected-password"),
            ) as load_credentials,
            patch("dango.config.helpers.is_cloud_mode", return_value=False),
            patch("dango.platform.local.network.NetworkConfig.get_project_info", return_value=None),
        ):
            _apply_metabase_site_url_catchup(session, "http://metabase.test", tmp_path)

        load_credentials.assert_called_once_with(tmp_path)
        assert session.post.call_args.kwargs["json"] == {
            "username": "admin@example.com",
            "password": "protected-password",
        }

    def test_site_url_catchup_without_credential_does_not_login(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import _apply_metabase_site_url_catchup

        _write_metadata(tmp_path)
        session = MagicMock()

        with (
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials", return_value=None
            ),
            patch("dango.config.helpers.is_cloud_mode", return_value=False),
            patch("dango.platform.local.network.NetworkConfig.get_project_info", return_value=None),
        ):
            _apply_metabase_site_url_catchup(session, "http://metabase.test", tmp_path)

        session.post.assert_not_called()
        session.put.assert_not_called()

    def test_schema_sync_uses_protected_credential(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import sync_metabase_schema

        _write_metadata(tmp_path)
        session = MagicMock()
        login_response = MagicMock(status_code=200)
        login_response.json.return_value = {"id": "protected-session"}
        sync_response = MagicMock(status_code=200)
        metadata_response = MagicMock(status_code=200)
        metadata_response.json.return_value = {
            "tables": [{"id": 1, "name": "orders", "schema": "marts"}]
        }
        session.post.side_effect = [login_response, sync_response]
        baseline_response = MagicMock(status_code=500)
        session.get.side_effect = [baseline_response, metadata_response]
        session.put.return_value = MagicMock(status_code=200)

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=session),
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials",
                return_value=("admin@example.com", "protected-password"),
            ),
            patch("dango.visualization.metabase.time.sleep"),
        ):
            assert sync_metabase_schema(tmp_path) is True

        assert session.post.call_args_list[0].kwargs["json"] == {
            "username": "admin@example.com",
            "password": "protected-password",
        }

    def test_schema_sync_without_credential_returns_false(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import sync_metabase_schema

        _write_metadata(tmp_path)
        session = MagicMock()
        with (
            patch("dango.visualization.metabase.requests.Session", return_value=session),
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials", return_value=None
            ),
        ):
            assert sync_metabase_schema(tmp_path) is False

        session.post.assert_not_called()

    def test_telemetry_uses_protected_credential(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import set_metabase_telemetry

        _write_metadata(tmp_path)
        session = MagicMock()
        session.post.return_value.json.return_value = {"id": "protected-session"}
        session.post.return_value.raise_for_status.return_value = None
        session.put.return_value.raise_for_status.return_value = None

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=session),
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials",
                return_value=("admin@example.com", "protected-password"),
            ),
        ):
            set_metabase_telemetry(tmp_path, False)

        assert session.post.call_args.kwargs["json"] == {
            "username": "admin@example.com",
            "password": "protected-password",
        }

    def test_telemetry_without_credential_has_clean_error(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import set_metabase_telemetry

        _write_metadata(tmp_path)
        with patch(
            "dango.visualization.metabase.load_metabase_admin_credentials", return_value=None
        ):
            with pytest.raises(click.ClickException, match="credentials are unavailable"):
                set_metabase_telemetry(tmp_path, False)

    def test_refresh_uses_protected_credential(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import refresh_metabase_connection

        _write_metadata(tmp_path)
        session = MagicMock()
        login_response = MagicMock(status_code=200)
        login_response.json.return_value = {"id": "protected-session"}
        session.post.return_value = login_response
        running = MagicMock(stdout="dango-test-metabase-1\n")
        restarted = MagicMock(returncode=0, stderr="")

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=session),
            patch("dango.platform.docker.DockerManager") as docker_manager,
            patch("dango.visualization.metabase.subprocess.run", side_effect=[running, restarted]),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
            patch("dango.visualization.metabase._apply_metabase_site_url_catchup"),
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials",
                return_value=("admin@example.com", "protected-password"),
            ),
        ):
            docker_manager.return_value.compose_project_name = "dango-test"
            result = refresh_metabase_connection(tmp_path)

        assert result == (True, None, "protected-session")
        assert session.post.call_args.kwargs["json"] == {
            "username": "admin@example.com",
            "password": "protected-password",
        }

    def test_refresh_without_credential_does_not_login(self, tmp_path: Path) -> None:
        from dango.visualization.metabase import refresh_metabase_connection

        _write_metadata(tmp_path)
        session = MagicMock()
        running = MagicMock(stdout="dango-test-metabase-1\n")
        restarted = MagicMock(returncode=0, stderr="")

        with (
            patch("dango.visualization.metabase.requests.Session", return_value=session),
            patch("dango.platform.docker.DockerManager") as docker_manager,
            patch("dango.visualization.metabase.subprocess.run", side_effect=[running, restarted]),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
            patch("dango.visualization.metabase._apply_metabase_site_url_catchup"),
            patch(
                "dango.visualization.metabase.load_metabase_admin_credentials", return_value=None
            ),
        ):
            docker_manager.return_value.compose_project_name = "dango-test"
            result = refresh_metabase_connection(tmp_path)

        assert result == (True, None, None)
        session.post.assert_not_called()
