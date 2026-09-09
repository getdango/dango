"""tests/unit/test_docker_identity_guard.py

Tests for the Docker project-identity collision guard added in 1.0.8-Q8.

Background: get_compose_project_name() derives a project's Docker Compose
identity from an MD5 hash of its path string — not a stable identifier. On
2026-09-09 this contributed to a real incident where a real project's
Metabase data was destroyed during manual cleanup of what looked like an
orphaned scratch project, based on an insufficiently-verified assumption
about which project a compose project name actually belonged to.

These are unit tests with mocked ``subprocess.run``. Live verification
against a real Docker daemon (two real scratch directories forced to share
one compose project name) was performed manually — see the PR description
for the exact commands and output; that live check cannot be run in CI
because it requires a real Docker daemon and mutates real containers.
"""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.exceptions import DockerIdentityCollisionError
from dango.platform.docker import DockerManager, _get_existing_container_working_dirs


@pytest.mark.unit
class TestGetExistingContainerWorkingDirs:
    def test_no_collision_when_no_existing_containers(self):
        """Empty docker ps result -> empty set, no exception."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            result = _get_existing_container_working_dirs("dango-abc123")
        assert result == set()

    def test_uses_dot_label_format_not_index_labels(self):
        """Regression guard for the exact --format syntax.

        Live-verified against Docker 28.5.1 / Compose v2.40.2: the
        prompt-proposed ``{{index .Labels "..."}}`` syntax FAILS at runtime
        on `docker ps` (`.Labels` is a pre-joined "k=v,k=v" string on this
        subcommand, not a map — `index` cannot operate on it). The correct
        syntax is the ``.Label "<key>"`` method. This test locks in the
        working syntax so a future "cleanup" doesn't silently reintroduce
        the broken one (which would make the guard permanently fail open,
        the exact class of unverified assumption this task exists to
        eliminate).
        """
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            _get_existing_container_working_dirs("dango-abc123")

        call_args = mock_run.call_args[0][0]
        format_arg = call_args[call_args.index("--format") + 1]
        assert format_arg == '{{.Label "com.docker.compose.project.working_dir"}}'
        assert "index" not in format_arg
        assert ".Labels" not in format_arg

    def test_returns_distinct_working_dirs(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="/path/a\n/path/a\n/path/b\n")
            result = _get_existing_container_working_dirs("dango-abc123")
        assert result == {"/path/a", "/path/b"}

    def test_fails_open_on_nonzero_returncode(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _get_existing_container_working_dirs("dango-abc123")
        assert result == set()

    def test_fails_open_on_timeout(self):
        import subprocess as subprocess_module

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess_module.TimeoutExpired(cmd="docker", timeout=10)
            result = _get_existing_container_working_dirs("dango-abc123")
        assert result == set()

    def test_fails_open_on_missing_docker_cli(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("docker not found")
            result = _get_existing_container_working_dirs("dango-abc123")
        assert result == set()


@pytest.mark.unit
class TestAssertNoIdentityCollision:
    def test_no_collision_when_no_existing_containers(self, tmp_path):
        """No existing containers under this project name -> guard passes silently."""
        manager = DockerManager(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            manager._assert_no_identity_collision()  # must not raise

    def test_no_collision_when_working_dir_matches(self, tmp_path):
        """Existing containers under this project name whose working_dir
        matches the current project root -> guard passes."""
        manager = DockerManager(tmp_path)
        current = str(tmp_path.resolve())
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{current}\n")
            manager._assert_no_identity_collision()  # must not raise

    def test_raises_on_working_dir_mismatch(self, tmp_path):
        """Existing containers under this project name with a *different*
        working_dir -> DockerIdentityCollisionError, with the mismatched
        path in the message."""
        manager = DockerManager(tmp_path)
        other_path = "/some/other/project/directory"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{other_path}\n")
            with pytest.raises(DockerIdentityCollisionError) as exc_info:
                manager._assert_no_identity_collision()

        assert other_path in str(exc_info.value)
        assert manager.compose_project_name in str(exc_info.value)

    def test_guard_fails_open_on_docker_error(self, tmp_path):
        """docker ps subprocess raises/times out -> guard does not raise,
        proceeds silently (fail-open)."""
        import subprocess as subprocess_module

        manager = DockerManager(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess_module.TimeoutExpired(cmd="docker", timeout=10)
            manager._assert_no_identity_collision()  # must not raise

    def test_partial_mismatch_still_raises(self, tmp_path):
        """If the existing set contains BOTH the current working_dir and a
        mismatched one (e.g. stale + fresh containers), the mismatch must
        still be caught rather than short-circuited by the match."""
        manager = DockerManager(tmp_path)
        current = str(tmp_path.resolve())
        other_path = "/some/other/project/directory"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{current}\n{other_path}\n")
            with pytest.raises(DockerIdentityCollisionError):
                manager._assert_no_identity_collision()


@pytest.mark.unit
class TestStartStopCallTheGuard:
    """start_services()/stop_services() must call the guard before doing
    anything else, and must let a confirmed collision propagate as an
    exception rather than swallowing it into a bool return."""

    @patch("dango.platform.docker.console")
    def test_start_services_raises_before_touching_docker(self, _mock_console, tmp_path):
        manager = DockerManager(tmp_path)
        other_path = "/some/other/project/directory"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{other_path}\n")
            with pytest.raises(DockerIdentityCollisionError):
                manager.start_services()

        # Only the guard's own `docker ps` call should have happened — no
        # `docker compose up` subprocess call.
        assert mock_run.call_count == 1
        called_cmd = mock_run.call_args_list[0][0][0]
        assert "ps" in called_cmd

    @patch("dango.platform.docker.console")
    def test_stop_services_raises_before_touching_docker(self, _mock_console, tmp_path):
        manager = DockerManager(tmp_path)
        other_path = "/some/other/project/directory"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{other_path}\n")
            with pytest.raises(DockerIdentityCollisionError):
                manager.stop_services()

        assert mock_run.call_count == 1
        called_cmd = mock_run.call_args_list[0][0][0]
        assert "ps" in called_cmd

    @patch("dango.platform.docker.console")
    def test_start_services_proceeds_normally_with_no_collision(self, _mock_console, tmp_path):
        """Regression check: the overwhelmingly common case (no existing
        containers under this project name) must proceed exactly as
        before — the guard adds one fast `docker ps` call, nothing else."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)

        with (
            patch("subprocess.run") as mock_run,
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch.object(manager, "_print_service_urls"),
        ):
            # Only one real subprocess.run call is left unmocked-via-patch.object:
            # the guard's `docker ps` (empty = no collision), followed by the
            # actual `docker compose ... up -d` call.
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            result = manager.start_services()

        assert result is True
        assert mock_run.call_count == 2


@pytest.mark.unit
class TestStopServicesHonestReporting:
    @patch("dango.platform.docker.console")
    def test_stop_services_reports_failure_when_containers_survive_down(
        self, _mock_console, tmp_path
    ):
        """`docker compose down` reports exit 0, but a follow-up `docker
        ps` check still finds containers under this project name ->
        stop_services() must not print/return unqualified success."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)

        with (
            patch("subprocess.run") as mock_run,
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # guard: no collision
                MagicMock(returncode=0, stdout="", stderr=""),  # docker compose down
                MagicMock(returncode=0, stdout="/some/leftover/dir\n"),  # post-down verify
            ]
            result = manager.stop_services()

        assert result is False

    @patch("dango.platform.docker.console")
    def test_stop_services_reports_success_when_containers_actually_gone(
        self, _mock_console, tmp_path
    ):
        """Sanity check for the same code path: when the post-down
        verification finds nothing, stop_services() still reports True."""
        (tmp_path / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(tmp_path)

        with (
            patch("subprocess.run") as mock_run,
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # guard: no collision
                MagicMock(returncode=0, stdout="", stderr=""),  # docker compose down
                MagicMock(returncode=0, stdout=""),  # post-down verify: nothing left
            ]
            result = manager.stop_services()

        assert result is True


@pytest.mark.unit
class TestCLILayerCatchesCollision:
    """`dango stop` must present a clean, structured error for a confirmed
    identity collision — never a raw traceback. `dango start` is not
    exercised here (its setup requires far more mocking of unrelated
    startup steps); the exception-handling wiring is identical in shape for
    both commands (see cli/commands/platform.py)."""

    def test_stop_prints_structured_error_not_traceback(self, tmp_path):
        from dango.cli.commands.platform import stop

        mock_config = MagicMock()
        mock_config.project.name = "test-project"

        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader") as mock_loader_cls,
            patch(
                "dango.platform.local.watcher_lifecycle.get_watcher_status",
                return_value={"running": False},
            ),
            patch("dango.platform.local.watcher_lifecycle.stop_file_watcher", return_value=True),
            patch("dango.platform.local.watcher_lifecycle.kill_orphan_watchers", return_value=0),
            patch("dango.notebooks.manager.get_marimo_status", return_value={"running": False}),
            patch("dango.notebooks.manager.stop_idle_checker"),
            patch("dango.notebooks.manager.stop_marimo", return_value=True),
            patch("dango.cli.helpers.process_manager.stop_fastapi_server"),
            patch("dango.platform.DockerManager") as mock_manager_cls,
        ):
            mock_loader_cls.return_value.load_config.return_value = mock_config
            mock_manager_cls.return_value.stop_services.side_effect = DockerIdentityCollisionError(
                "Compose project 'dango-abc123' already has containers belonging to a "
                "different project directory: ['/some/other/path']."
            )

            runner = CliRunner()
            result = runner.invoke(stop, obj={})

        assert result.exit_code != 0
        assert "Traceback" not in result.output
        assert "Docker project-identity collision detected" in result.output
        assert "dango-abc123" in result.output
