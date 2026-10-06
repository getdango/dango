"""tests/integration/test_dashboard_provision_noflags.py

Release-only real-Docker check (1.0.13-T4, C10): on a fresh project, `dango dashboard provision`
with no flags and stdin closed logs in with the stored Metabase credential and creates the
"Data Pipeline Health" dashboard, instead of prompting for a password nobody knows.
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

_ADMIN_EMAIL = "admin@dango-provision-noflags.test"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


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


def _dango(project_root: Path, env: dict[str, str], *args: str, timeout: int) -> tuple[int, str]:
    """Run a real `dango` subprocess with stdin closed; return (exit code, clean output)."""
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


def _cleanup(project_root: Path, compose_project_name: str, env: dict[str, str]) -> None:
    """Tear the temporary project down and assert nothing of it is left in Docker.

    ``down`` runs with the REAL environment (the fake HOME hides the `docker compose` plugin),
    and ``--rmi local`` also removes the built Metabase image.
    """
    try:
        _dango(project_root, env, "stop", timeout=180)
    except Exception as exc:  # noqa: BLE001 - a stuck `stop` must not skip the Docker teardown
        print(f"dango stop failed during teardown: {exc}", file=sys.stderr)
    if not compose_project_name:
        return
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
    assert down.returncode == 0, f"docker compose down failed: {down.stderr[-1500:]}"
    volumes = subprocess.run(
        [
            "docker",
            "volume",
            "ls",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={compose_project_name}",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert volumes.stdout.strip() == "", f"leaked volumes: {volumes.stdout}"
    image = subprocess.run(
        ["docker", "image", "inspect", f"{compose_project_name}-metabase"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert image.returncode != 0, f"leaked image {compose_project_name}-metabase"


@pytest.mark.integration
class TestDashboardProvisionNoFlags:
    """`dango dashboard provision` needs no flags once Metabase is configured."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_provision_with_no_flags_succeeds_against_real_metabase(
        self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import keyring
        import requests
        import yaml
        from keyring.backends.fail import Keyring as NoKeyring

        from dango.cli.init import init_project
        from dango.platform import DockerManager
        from dango.security import metabase_credentials
        from dango.security.metabase_config import load_metabase_admin_credentials

        project_root = tmp_path_factory.mktemp("provision-noflags")
        fake_home = tmp_path_factory.mktemp("provision-noflags-home")
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
            "DANGO_ADMIN_EMAIL": _ADMIN_EMAIL,
        }
        compose_project_name = ""
        try:
            init_project(project_root, skip_wizard=True)
            _configure_unique_docker_ports(project_root)
            compose_project_name = DockerManager(project_root).compose_project_name

            code, out = _dango(project_root, env, "start", "-y", timeout=900)
            assert code == 0, f"dango start failed ({code}):\n{out[-3000:]}"

            code, out = _dango(project_root, env, "dashboard", "provision", timeout=300)
            assert code == 0, f"provision failed ({code}):\n{out[-3000:]}"
            assert "Dashboard provisioned successfully" in out, out[-3000:]

            creds = load_metabase_admin_credentials(project_root)
            assert creds is not None, "no stored Metabase credential after start"
            email, password = creds
            url = yaml.safe_load((project_root / ".dango" / "metabase.yml").read_text())[
                "metabase_url"
            ]
            session = requests.post(
                f"{url}/api/session", json={"username": email, "password": password}, timeout=30
            )
            assert session.status_code == 200, session.text
            dashboards = requests.get(
                f"{url}/api/dashboard",
                headers={"X-Metabase-Session": str(session.json()["id"])},
                timeout=30,
            ).json()
            assert any(d.get("name") == "Data Pipeline Health" for d in dashboards), dashboards
        finally:
            keyring.set_keyring(previous_backend)
            try:
                _cleanup(project_root, compose_project_name, env)
            except BaseException as teardown_error:  # noqa: BLE001
                if sys.exc_info()[0] is None:
                    raise
                # Do not mask the test's real failure with a teardown error.
                print(f"teardown error: {teardown_error!r}", file=sys.stderr)
