"""tests/integration/test_custom_metabase_port.py

Real-Docker check (1.0.13-T5 / C2): with a custom ``metabase_port``, `dango start` verifies the
right Metabase and the host-side Metabase commands target the configured port, not 3000.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_ADMIN_EMAIL = "admin@dango-t5.test"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _free_port() -> int:
    """Find a free localhost port that is never 3000."""
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        if port != 3000:
            return port


def _configure_unique_docker_ports(project_root: Path) -> int:
    """Assign isolated ports, re-render the real Compose file, return the Metabase port."""
    from dango.cli.init import ProjectInitializer
    from dango.config import ConfigLoader

    loader = ConfigLoader(project_root)
    config = loader.load_config()
    config.platform.port = _free_port()
    config.platform.metabase_port = _free_port()
    config.platform.dbt_docs_port = _free_port()
    loader.save_config(config)
    ProjectInitializer(project_root)._create_docker_compose(config)
    return int(config.platform.metabase_port)


def _dango(
    args: list[str], project_root: Path, env: dict[str, str], timeout: int
) -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode, _ANSI_RE.sub("", result.stdout + result.stderr)


def _docker_names(*args: str) -> list[str]:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, env=dict(os.environ)
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def _teardown(project_root: Path, compose_project_name: str, fake_home_env: dict[str, str]) -> None:
    """Stop dango (fake HOME) then tear Docker down with the REAL environment.

    Under the fake HOME the compose plugin does not resolve, so `down -v` would silently leak
    the volume; `--rmi local` also removes the project's built image.
    """
    _dango(["stop"], project_root, fake_home_env, 180)
    down = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(project_root / "docker-compose.yml"),
            "down",
            "-v",
            "--rmi",
            "local",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "COMPOSE_PROJECT_NAME": compose_project_name},
    )
    assert down.returncode == 0, f"docker compose down failed:\n{down.stdout}\n{down.stderr}"
    leftovers = (
        _docker_names(
            "ps", "-a", "--format", "{{.Names}}", "--filter", f"name={compose_project_name}"
        )
        + _docker_names("volume", "ls", "-q", "--filter", f"name={compose_project_name}")
        + [
            ref
            for ref in _docker_names("images", "--format", "{{.Repository}}:{{.Tag}}")
            if compose_project_name in ref
        ]
    )
    assert not leftovers, f"Docker resources left behind by this test: {leftovers}"


@pytest.mark.integration
def test_custom_metabase_port_is_used_by_start_and_metabase_commands(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
    if shutil.which("docker") is None:
        pytest.skip("Docker not available")

    import keyring
    from keyring.backends.fail import Keyring as NoKeyring

    from dango.cli.init import init_project
    from dango.platform import DockerManager
    from dango.security import metabase_credentials
    from dango.security.metabase_config import resolve_metabase_url

    project_root = tmp_path_factory.mktemp("custom-metabase-port")
    fake_home = tmp_path_factory.mktemp("custom-metabase-port-home")
    previous_backend = keyring.get_keyring()
    keyring.set_keyring(NoKeyring())
    monkeypatch.setattr(
        metabase_credentials,
        "_LOCAL_SECRETS_DIR",
        fake_home / ".dango" / "secrets" / "metabase",
    )
    monkeypatch.setenv("DANGO_ADMIN_EMAIL", _ADMIN_EMAIL)
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "BROWSER": "true",
        "DANGO_LOG_LEVEL": "ERROR",
    }
    compose_project_name = ""
    try:
        init_project(project_root, skip_wizard=True)
        metabase_port = _configure_unique_docker_ports(project_root)
        assert metabase_port != 3000
        # Computed before Docker starts so teardown can run even if `start` fails.
        compose_project_name = DockerManager(project_root).compose_project_name

        code, start_output = _dango(["start", "-y"], project_root, env, 900)
        assert code == 0, f"dango start failed ({code}):\n{start_output[-3000:]}"
        assert "Metabase ready" in start_output, start_output[-3000:]

        assert resolve_metabase_url(project_root).endswith(f":{metabase_port}")

        code, save_output = _dango(["metabase", "save"], project_root, env, 300)
        assert code == 0, f"dango metabase save failed ({code}):\n{save_output[-3000:]}"
        assert "localhost:3000" not in save_output, save_output
    finally:
        try:
            if compose_project_name:
                _teardown(project_root, compose_project_name, env)
        finally:
            keyring.set_keyring(previous_backend)
