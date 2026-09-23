"""tests/unit/test_docker.py

Tests for dango.platform.docker — Docker Compose lifecycle management.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.docker import DockerManager, ServiceStatus


@pytest.mark.unit
class TestMetabaseImageDetection:
    def test_uses_project_scoped_compose_image_name(self, tmp_path):
        """The image query uses Compose's actual project-scoped image name."""
        manager = DockerManager(tmp_path)

        with (
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-59f02899"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="image-id\n")
            assert manager._metabase_image_exists() is True

        assert mock_run.call_args.args[0] == [
            "docker",
            "images",
            "--filter",
            "reference=dango-59f02899-metabase",
            "-q",
        ]


@pytest.mark.unit
class TestStartServicesTimeoutDiagnostics:
    @patch("dango.platform.docker.console")
    def test_timeout_prints_partial_stdout_and_stderr(self, mock_console, tmp_path):
        """Compose output captured before timeout remains available for diagnosis."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)
        timeout = subprocess.TimeoutExpired(
            cmd="docker compose up -d",
            timeout=120,
            output="Container dango-metabase Creating\n",
            stderr="waiting for Docker daemon\n",
        )

        with (
            patch.object(manager, "_resolve_or_migrate_project_id"),
            patch.object(manager, "_assert_no_identity_collision"),
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch("subprocess.run", side_effect=timeout),
        ):
            assert manager.start_services() is False

        printed = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "Timeout starting Docker services" in printed
        assert "Partial output before timeout" in printed
        assert "Container dango-metabase Creating" in printed
        assert "Partial error output before timeout" in printed
        assert "waiting for Docker daemon" in printed

    @patch("dango.platform.docker.console")
    def test_timeout_decodes_bytes_output(self, mock_console, tmp_path):
        """Timeout diagnostics decode bytes returned by subprocess safely."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)
        timeout = subprocess.TimeoutExpired(
            cmd="docker compose up -d", timeout=120, output=b"created\n", stderr=b"delayed\n"
        )

        with (
            patch.object(manager, "_resolve_or_migrate_project_id"),
            patch.object(manager, "_assert_no_identity_collision"),
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch.object(manager, "get_service_status", return_value={}),
            patch("subprocess.run", side_effect=timeout),
        ):
            assert manager.start_services() is False

        printed = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "created" in printed
        assert "delayed" in printed
        assert "b'" not in printed

    @patch("dango.platform.docker.console")
    def test_timeout_without_output_still_returns_false(self, mock_console, tmp_path):
        """Missing partial streams do not hide an ordinary startup timeout."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)
        timeout = subprocess.TimeoutExpired(cmd="docker compose up -d", timeout=120)

        with (
            patch.object(manager, "_resolve_or_migrate_project_id"),
            patch.object(manager, "_assert_no_identity_collision"),
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch.object(manager, "get_service_status", return_value={}),
            patch("subprocess.run", side_effect=timeout),
        ):
            assert manager.start_services() is False

        printed = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "Timeout starting Docker services" in printed
        assert "Partial output before timeout" not in printed

    @patch("dango.platform.docker.console")
    def test_timeout_returns_success_when_both_services_are_healthy(self, mock_console, tmp_path):
        """A late Compose timeout does not trigger cleanup of healthy services."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)
        timeout = subprocess.TimeoutExpired(cmd="docker compose up -d", timeout=120)

        with (
            patch.object(manager, "_resolve_or_migrate_project_id"),
            patch.object(manager, "_assert_no_identity_collision"),
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch.object(
                manager,
                "get_service_status",
                return_value={
                    "metabase": ServiceStatus.RUNNING,
                    "dbt-docs": ServiceStatus.STARTING,
                },
            ),
            patch("subprocess.run", side_effect=timeout),
        ):
            assert manager.start_services() is True

        printed = "\n".join(
            str(call.args[0]) for call in mock_console.print.call_args_list if call.args
        )
        assert "continuing without cleanup" in printed

    @patch("dango.platform.docker.console")
    def test_nonzero_compose_result_remains_a_failure(self, _mock_console, tmp_path):
        """Only a reconciled timeout succeeds; a normal Compose error still fails."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)

        with (
            patch.object(manager, "_resolve_or_migrate_project_id"),
            patch.object(manager, "_assert_no_identity_collision"),
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stdout="", stderr="invalid compose config"),
            ),
        ):
            assert manager.start_services() is False


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
