"""tests/unit/test_docker.py

Tests for dango.platform.docker — Docker Compose lifecycle management.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.docker import DockerManager


@pytest.mark.unit
class TestStopAllDangoContainers:
    @patch("dango.platform.docker.console")
    def test_default_stops_containers_for_current_project(self, _mock_console, tmp_path):
        """Default call (all_projects=False) filters by compose project label."""
        manager = DockerManager(tmp_path)
        project_name = manager.compose_project_name

        with patch("subprocess.run") as mock_run:
            # docker ps returns empty (no containers found)
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            manager.stop_all_dango_containers()

        # Verify docker ps was called with project label filter
        ps_call = mock_run.call_args_list[0]
        ps_cmd = ps_call[0][0]
        assert "docker" in ps_cmd[0]
        assert "ps" in ps_cmd[1]
        assert "--filter" in ps_cmd
        assert f"label=com.docker.compose.project={project_name}" in ps_cmd

    @patch("dango.platform.docker.console")
    def test_all_projects_true_uses_name_filter(self, _mock_console, tmp_path):
        """all_projects=True uses the global name-based filter (metabase, dbt)."""
        manager = DockerManager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            manager.stop_all_dango_containers(all_projects=True)

        ps_call = mock_run.call_args_list[0]
        ps_cmd = ps_call[0][0]
        # Should filter by name instead of label
        assert "name=metabase" in ps_cmd
        assert "name=dbt" in ps_cmd

    @patch("dango.platform.docker.console")
    def test_stops_found_containers(self, _mock_console, tmp_path):
        """Found container IDs are passed to docker stop."""
        manager = DockerManager(tmp_path)

        with patch("subprocess.run") as mock_run:
            # First call (docker ps) returns container IDs, second (docker stop) succeeds
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123\ndef456\n"),
                MagicMock(returncode=0, stdout=""),
            ]

            result = manager.stop_all_dango_containers()

        assert result is True

        # Second call should be docker stop with the container IDs
        stop_call = mock_run.call_args_list[1]
        stop_cmd = stop_call[0][0]
        assert "docker" in stop_cmd[0]
        assert "stop" in stop_cmd[1]
        assert "abc123" in stop_cmd
        assert "def456" in stop_cmd

    @patch("dango.platform.docker.console")
    def test_no_containers_found(self, _mock_console, tmp_path):
        """Returns True when no containers match the filter."""
        manager = DockerManager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")

            result = manager.stop_all_dango_containers()

        assert result is True
        # Only docker ps was called, not docker stop
        assert mock_run.call_count == 1

    @patch("dango.platform.docker.console")
    def test_docker_ps_failure_returns_false(self, _mock_console, tmp_path):
        """Non-zero returncode from docker ps returns False."""
        manager = DockerManager(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")

            result = manager.stop_all_dango_containers()

        assert result is False

    @patch("dango.platform.docker.console")
    def test_timeout_returns_false(self, _mock_console, tmp_path):
        """TimeoutExpired from subprocess is caught and returns False."""
        manager = DockerManager(tmp_path)

        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="docker ps", timeout=10),
        ):
            result = manager.stop_all_dango_containers()

        assert result is False

    @patch("dango.platform.docker.console")
    def test_generic_exception_returns_false(self, _mock_console, tmp_path):
        """Any exception is caught and returns False gracefully."""
        manager = DockerManager(tmp_path)

        with patch("subprocess.run", side_effect=RuntimeError("unexpected")):
            result = manager.stop_all_dango_containers()

        assert result is False
