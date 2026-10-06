"""tests/integration/docker_leak_support.py

Teardown and leak checks for real-Docker integration tests: remove one temp project's
containers, volume and built image, then fail if anything labelled with it is left behind.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _docker(args: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], cwd=cwd, capture_output=True, text=True, timeout=180, env=env
    )
    if result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def compose_down_and_prune(project_root: Path, compose_project_name: str) -> None:
    """Run `compose down -v --rmi local` for this project only, with the REAL environment.

    Never pass a test's fake HOME here: it hides the `docker compose` plugin and the
    teardown then fails silently (the 1.0.12 volume/image leak).
    """
    compose_file = project_root / "docker-compose.yml"
    if not compose_file.exists():
        return
    env = {**os.environ, "COMPOSE_PROJECT_NAME": compose_project_name}
    _docker(["compose", "-f", str(compose_file), "down", "-v", "--rmi", "local"], project_root, env)


def assert_no_docker_leftovers(compose_project_name: str) -> None:
    """Fail with the offending names if this project's volume or image still exists."""
    label = f"label=com.docker.compose.project={compose_project_name}"
    leftovers = _docker(["volume", "ls", "-q", "--filter", label]).split()
    image = f"{compose_project_name}-metabase"
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, timeout=30)
    if probe.returncode == 0:
        leftovers.append(f"image:{image}")
    assert not leftovers, f"Docker resources leaked by {compose_project_name}: {leftovers}"
