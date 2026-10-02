"""tests/integration/test_docker_identity_migration.py

Release-only real-Docker checks for upgrading project Compose identities.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration


def _free_port() -> int:
    """Find an isolated localhost port for a temporary Docker project."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configure_unique_docker_ports(project_root: Path) -> None:
    """Assign isolated ports and re-render the real Compose file."""
    from dango.cli.init import ProjectInitializer
    from dango.config import ConfigLoader

    loader = ConfigLoader(project_root)
    config = loader.load_config()
    config.platform.port = _free_port()
    config.platform.metabase_port = _free_port()
    config.platform.dbt_docs_port = _free_port()
    loader.save_config(config)
    ProjectInitializer(project_root)._create_docker_compose(config)


def _inspect_docker_volume(name: str) -> dict[str, Any]:
    """Read a real Docker volume or fail with Docker's diagnostic output."""
    result = subprocess.run(
        ["docker", "volume", "inspect", name], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, f"Could not inspect Docker volume {name!r}: {result.stderr}"
    volumes = json.loads(result.stdout)
    assert len(volumes) == 1, f"Expected one Docker volume named {name!r}: {volumes}"
    return volumes[0]


def _cleanup(project_root: Path, compose_project_name: str) -> None:
    """Best-effort cleanup for a temporary release-readiness project only."""
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = compose_project_name
    subprocess.run(
        ["docker", "compose", "-f", str(project_root / "docker-compose.yml"), "down", "-v"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _compose_command(project_root: Path) -> list[str]:
    """Return the base `docker compose -f <project>/docker-compose.yml` command."""
    return ["docker", "compose", "-f", str(project_root / "docker-compose.yml")]


def _compose_env(compose_project_name: str) -> dict[str, str]:
    """Return the environment Dango itself uses to name the Compose project."""
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = compose_project_name
    return env


def _legacy_compose_without_volume_labels(current: str) -> str:
    """Return the compose file as releases before 1.0.8-Q12 rendered it (no volume labels)."""
    legacy = re.sub(
        r"\n    labels:\n      com\.dango\.project_name: .*\n      com\.dango\.project_id: .*",
        "",
        current,
    )
    assert legacy != current, "docker-compose.yml.j2 no longer declares the volume labels"
    assert "com.dango" not in legacy
    return legacy


def _write_volume_sentinel(volume_name: str) -> None:
    """Write a marker file into the volume; it only survives if the volume is never recreated."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume_name}:/v",
            "nginx:alpine",
            "-c",
            "echo keep > /v/sentinel",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"Could not write the volume sentinel: {result.stderr}"


def _read_volume_sentinel(volume_name: str) -> str:
    """Return the sentinel file's contents, or an empty string if it is gone."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume_name}:/v",
            "nginx:alpine",
            "-c",
            "cat /v/sentinel",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _start_services_under_terminal(project_root: Path, timeout: int = 300) -> tuple[int, str]:
    """Run `DockerManager.start_services()` in a child whose stdin is a real pseudo-terminal.

    This is the condition under which Docker Compose's interactive "Recreate (data will be
    lost)?" prompt blocks: a non-terminal stdin makes Compose answer No immediately, so a plain
    subprocess call cannot reproduce the hang. Returns (exit code, combined output).
    """
    import pty

    import dango

    child_code = (
        "import sys; from pathlib import Path; from dango.platform import DockerManager; "
        "sys.exit(0 if DockerManager(Path(sys.argv[1])).start_services() else 1)"
    )
    child_env = os.environ.copy()
    # Import the same `dango` this test imported (a worktree, not the venv's editable install).
    child_env["PYTHONPATH"] = str(Path(dango.__file__).resolve().parent.parent)
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code, str(project_root)],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=child_env,
        )
        os.close(slave_fd)
        slave_fd = -1
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            pytest.fail(
                f"dango start blocked for {timeout}s with a terminal on stdin "
                "(Docker Compose is probably waiting on its volume-recreate prompt)"
            )
        return proc.returncode, output
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)


class TestDockerIdentityMigration:
    """A stopped pre-1.0.8 project must retain its original data volume."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_volume_only_pre_108_project_keeps_legacy_identity_on_upgrade(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A legacy stopped project resumes with its existing Metabase volume."""
        from dango.cli.init import init_project
        from dango.config import ConfigLoader
        from dango.platform import DockerManager
        from dango.platform.docker import _legacy_path_hash

        project_root = tmp_path_factory.mktemp("release-readiness-pre-108")
        init_project(project_root, skip_wizard=True)
        _configure_unique_docker_ports(project_root)
        loader = ConfigLoader(project_root)
        raw_project = loader.load_yaml(loader.project_file)
        raw_project["project"].pop("id", None)
        loader.save_yaml(raw_project, loader.project_file)

        legacy_name = f"dango-{_legacy_path_hash(project_root)}"
        volume_name = f"{legacy_name}_metabase-data"
        legacy_env = os.environ.copy()
        legacy_env["COMPOSE_PROJECT_NAME"] = legacy_name
        compose_cmd = ["docker", "compose", "-f", str(project_root / "docker-compose.yml")]

        try:
            legacy_start = subprocess.run(
                compose_cmd + ["up", "-d"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=600,
                env=legacy_env,
            )
            assert legacy_start.returncode == 0, legacy_start.stderr
            legacy_volume = _inspect_docker_volume(volume_name)
            legacy_stop = subprocess.run(
                compose_cmd + ["down"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=legacy_env,
            )
            assert legacy_stop.returncode == 0, legacy_stop.stderr
            assert _inspect_docker_volume(volume_name)["CreatedAt"] == legacy_volume["CreatedAt"]

            manager = DockerManager(project_root)
            assert manager.start_services(), "Current Dango could not resume the legacy project"
            assert manager.compose_project_name == legacy_name
            assert _inspect_docker_volume(volume_name)["CreatedAt"] == legacy_volume["CreatedAt"]
            migrated = loader.load_yaml(loader.project_file)
            assert migrated["project"]["id"] == _legacy_path_hash(project_root)
        finally:
            _cleanup(project_root, legacy_name)


class TestComposeVolumeConfigMismatch:
    """`dango start` must keep a Metabase volume whose config differs from docker-compose.yml.

    1.0.8-Q12 added `com.dango.*` labels to the volume. Docker Compose treats a labelled/unlabelled
    difference as a config mismatch and asks "Recreate (data will be lost)?"; with a terminal on
    stdin that prompt blocked `dango start` until it timed out (1.0.10 BUGS-FOUND D1/D2).
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_pre_q12_unlabelled_volume_survives_start_under_a_terminal(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A volume created without `com.dango.*` labels keeps its data under today's template."""
        from dango.cli.init import init_project
        from dango.platform import DockerManager

        project_root = tmp_path_factory.mktemp("compose-mismatch-pre-q12")
        init_project(project_root, skip_wizard=True)
        _configure_unique_docker_ports(project_root)
        compose_name = DockerManager(project_root).compose_project_name
        volume_name = f"{compose_name}_metabase-data"
        compose_file = project_root / "docker-compose.yml"
        current_compose = compose_file.read_text()

        try:
            compose_file.write_text(_legacy_compose_without_volume_labels(current_compose))
            created = subprocess.run(
                _compose_command(project_root) + ["up", "-d"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=600,
                env=_compose_env(compose_name),
            )
            assert created.returncode == 0, created.stderr
            legacy_volume = _inspect_docker_volume(volume_name)
            assert not any(key.startswith("com.dango.") for key in legacy_volume["Labels"])
            _write_volume_sentinel(volume_name)
            stopped = subprocess.run(
                _compose_command(project_root) + ["down"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=_compose_env(compose_name),
            )
            assert stopped.returncode == 0, stopped.stderr

            compose_file.write_text(current_compose)  # today's template, with volume labels
            returncode, output = _start_services_under_terminal(project_root)

            assert returncode == 0, f"start_services() failed under a terminal:\n{output}"
            kept_volume = _inspect_docker_volume(volume_name)
            assert kept_volume["CreatedAt"] == legacy_volume["CreatedAt"]
            assert not any(key.startswith("com.dango.") for key in kept_volume["Labels"]), (
                "The volume was recreated with the new labels (data would have been lost)"
            )
            assert _read_volume_sentinel(volume_name) == "keep", "Metabase volume data was lost"
        finally:
            _cleanup(project_root, compose_name)

    def test_renamed_project_keeps_its_labelled_volume_under_a_terminal(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """Editing `project.name` changes the volume label; the next start must still keep data."""
        from dango.cli.init import ProjectInitializer, init_project
        from dango.config import ConfigLoader
        from dango.platform import DockerManager

        project_root = tmp_path_factory.mktemp("compose-mismatch-rename")
        init_project(project_root, skip_wizard=True)
        _configure_unique_docker_ports(project_root)
        compose_name = DockerManager(project_root).compose_project_name
        volume_name = f"{compose_name}_metabase-data"

        try:
            created = subprocess.run(
                _compose_command(project_root) + ["up", "-d"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=600,
                env=_compose_env(compose_name),
            )
            assert created.returncode == 0, created.stderr
            original_volume = _inspect_docker_volume(volume_name)
            original_label = original_volume["Labels"]["com.dango.project_name"]
            _write_volume_sentinel(volume_name)
            stopped = subprocess.run(
                _compose_command(project_root) + ["down"],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=120,
                env=_compose_env(compose_name),
            )
            assert stopped.returncode == 0, stopped.stderr

            loader = ConfigLoader(project_root)
            config = loader.load_config()
            config.project.name = "renamed-compose-project"
            loader.save_config(config)
            ProjectInitializer(project_root)._create_docker_compose(config)
            assert (
                'com.dango.project_name: "renamed-compose-project"'
                in (project_root / "docker-compose.yml").read_text()
            )
            assert original_label != "renamed-compose-project"

            returncode, output = _start_services_under_terminal(project_root)

            assert returncode == 0, f"start_services() failed under a terminal:\n{output}"
            kept_volume = _inspect_docker_volume(volume_name)
            assert kept_volume["CreatedAt"] == original_volume["CreatedAt"]
            assert kept_volume["Labels"]["com.dango.project_name"] == original_label
            assert _read_volume_sentinel(volume_name) == "keep", "Metabase volume data was lost"
        finally:
            _cleanup(project_root, compose_name)
