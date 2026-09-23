"""tests/integration/test_docker_identity_migration.py

Release-only real-Docker checks for upgrading project Compose identities.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
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
            assert _inspect_docker_volume(volume_name)["Mountpoint"] == legacy_volume["Mountpoint"]

            manager = DockerManager(project_root)
            assert manager.start_services(), "Current Dango could not resume the legacy project"
            assert manager.compose_project_name == legacy_name
            assert _inspect_docker_volume(volume_name)["Mountpoint"] == legacy_volume["Mountpoint"]
            migrated = loader.load_yaml(loader.project_file)
            assert migrated["project"]["id"] == _legacy_path_hash(project_root)
        finally:
            _cleanup(project_root, legacy_name)
