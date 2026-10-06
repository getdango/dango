"""tests/integration/test_serve_credential_state.py

Real-Docker check (local project, not a cloud test) that metabase_admin_credential_state
classifies a live Metabase correctly: ok, then missing, then rejected after one login.
Teardown uses the real Docker environment and asserts nothing of the project is left over.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.integration

_ADMIN_EMAIL = "admin@dango-serve-state.test"


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


def _docker_lines(*args: str) -> list[str]:
    """Run a read-only docker query with the real environment and return its lines."""
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=60, env=dict(os.environ)
    )
    assert result.returncode == 0, result.stderr
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _teardown_and_assert_clean(project_root: Path, compose_project_name: str) -> None:
    """Remove only this project's Compose resources, then assert none are left."""
    assert compose_project_name.startswith("dango-"), compose_project_name
    # Real environment on purpose: a fake HOME makes docker look in the wrong place and leaks.
    env = {**os.environ, "COMPOSE_PROJECT_NAME": compose_project_name}
    result = subprocess.run(
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
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, f"docker compose down failed: {result.stderr[-2000:]}"

    containers = _docker_lines("ps", "-a", "--format", "{{.Names}}")
    volumes = _docker_lines("volume", "ls", "--format", "{{.Name}}")
    images = _docker_lines("image", "ls", "--format", "{{.Repository}}")
    leftovers = (
        [c for c in containers if c.startswith(f"{compose_project_name}-")]
        + [v for v in volumes if v.startswith(f"{compose_project_name}_")]
        + [i for i in images if i.startswith(f"{compose_project_name}-")]
    )
    assert not leftovers, f"Docker resources left behind: {leftovers}"


class TestServeCredentialState:
    """The three detection states against a real Metabase, with one login each at most."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_state_detection_against_real_metabase(
        self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import secrets

        import keyring
        import requests
        import yaml
        from keyring.backends.fail import Keyring as NoKeyring

        from dango.cli.init import init_project
        from dango.config import ConfigLoader
        from dango.platform import DockerManager
        from dango.platform.common.startup import (
            metabase_admin_credential_state,
            setup_metabase_if_needed,
            start_docker_services,
        )
        from dango.security import metabase_credentials
        from dango.security.metabase_config import load_metabase_admin_credentials
        from dango.security.metabase_credentials import MetabaseCredentialStore

        project_root = tmp_path_factory.mktemp("serve-credential-state")
        fake_home = tmp_path_factory.mktemp("serve-credential-state-home")
        previous_backend = keyring.get_keyring()
        keyring.set_keyring(NoKeyring())
        # dango/keyring isolation only: the credential store lives under the fake HOME.
        monkeypatch.setattr(
            metabase_credentials,
            "_LOCAL_SECRETS_DIR",
            fake_home / ".dango" / "secrets" / "metabase",
        )
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", _ADMIN_EMAIL)
        compose_project_name = ""
        try:
            init_project(project_root, skip_wizard=True)
            _configure_unique_docker_ports(project_root)
            config = ConfigLoader(project_root).load_config()
            # Derived before Docker starts so teardown can never depend on a started stack.
            compose_project_name = DockerManager(project_root).compose_project_name
            start_docker_services(project_root)
            setup = setup_metabase_if_needed(project_root, config.project.name, organization=None)
            assert setup.get("success"), f"Metabase setup failed: {setup}"

            mb_yml = project_root / ".dango" / "metabase.yml"
            metadata = yaml.safe_load(mb_yml.read_text())
            url = metadata["metabase_url"]
            email, password = load_metabase_admin_credentials(project_root) or ("", "")
            assert password, "setup did not store the Metabase admin password"
            assert "password" not in metadata["admin"], "the post-1.0.12 state has no YAML password"
            store = MetabaseCredentialStore(project_root)

            def state_with_login_count() -> tuple[str, int]:
                with patch("requests.post", wraps=requests.post) as post:
                    state = metabase_admin_credential_state(project_root)
                return state, post.call_count

            ok = state_with_login_count()
            print(f"STATE ok -> {ok}")
            assert ok == ("ok", 1)

            store.delete()
            assert store.load() is None
            missing = state_with_login_count()
            print(f"STATE missing -> {missing}")
            assert missing == ("missing", 0)

            # Restore the stored credential, then change the real admin password behind it.
            store.save(password)
            session = requests.post(
                f"{url}/api/session", json={"username": email, "password": password}, timeout=30
            )
            assert session.status_code == 200, session.text
            headers = {"X-Metabase-Session": str(session.json()["id"])}
            user_id = requests.get(f"{url}/api/user/current", headers=headers, timeout=30).json()[
                "id"
            ]
            changed = requests.put(
                f"{url}/api/user/{user_id}/password",
                headers=headers,
                json={"password": secrets.token_urlsafe(24), "old_password": password},
                timeout=30,
            )
            assert changed.status_code == 200, changed.text
            rejected = state_with_login_count()
            print(f"STATE rejected -> {rejected}")
            assert rejected == ("rejected", 1)
        finally:
            try:
                if compose_project_name:
                    _teardown_and_assert_clean(project_root, compose_project_name)
            finally:
                keyring.set_keyring(previous_backend)
