"""tests/integration/test_metabase_admin_state_matrix.py

Release-only real-Docker check of the Metabase admin credential states a long-lived project
can be in (1.0.12): a stale YAML password with a working SSO copy heals without touching
Metabase, and a fully lost credential is repaired automatically by `dango start` with every
Metabase object preserved.
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

_ADMIN_EMAIL = "admin@dango-admin-state.test"
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


def _cleanup(project_root: Path, env: dict[str, str]) -> None:
    """Teardown: stop via the CLI (fake HOME), then remove this project's Docker resources."""
    from dango.platform.docker import get_compose_project_name
    from tests.integration.docker_leak_support import teardown_and_check

    subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "stop"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    name = get_compose_project_name(project_root)  # derived here, so early failures still clean
    teardown_and_check(project_root, name)


def _dango_start(project_root: Path, env: dict[str, str]) -> str:
    """Run the real `dango start -y` as a subprocess (stdin closed) and return clean output."""
    result = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "start", "-y"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=900,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    output = _ANSI_RE.sub("", result.stdout + result.stderr)
    assert result.returncode == 0, f"dango start failed ({result.returncode}):\n{output[-3000:]}"
    return output


def _login(url: str, email: str, password: str) -> int:
    import requests

    return requests.post(
        f"{url}/api/session", json={"username": email, "password": password}, timeout=30
    ).status_code


def _session(url: str, email: str, password: str) -> str:
    import requests

    response = requests.post(
        f"{url}/api/session", json={"username": email, "password": password}, timeout=30
    )
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _stop(project_root: Path, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "stop"],
        cwd=project_root,
        capture_output=True,
        timeout=180,
        env=env,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.integration
class TestMetabaseAdminCredentialStates:
    """Stale-YAML-with-SSO-copy heals in place; a fully lost credential is repaired."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_stale_yaml_heals_then_lost_credential_is_repaired(
        self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import secrets

        import keyring
        import requests
        import yaml
        from keyring.backends.fail import Keyring as NoKeyring

        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import get_user_by_email
        from dango.cli.init import init_project
        from dango.config import ConfigLoader
        from dango.platform.common.startup import setup_metabase_if_needed, start_docker_services
        from dango.security import metabase_credentials
        from dango.security.metabase_config import load_metabase_admin_credentials
        from dango.security.metabase_credentials import MetabaseCredentialStore

        project_root = tmp_path_factory.mktemp("metabase-admin-states")
        fake_home = tmp_path_factory.mktemp("metabase-admin-states-home")
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
        mb_yml = project_root / ".dango" / "metabase.yml"
        try:
            init_project(project_root, skip_wizard=True)
            _configure_unique_docker_ports(project_root)
            config = ConfigLoader(project_root).load_config()
            start_docker_services(project_root)
            setup = setup_metabase_if_needed(project_root, config.project.name, organization=None)
            assert setup.get("success"), f"Metabase setup failed: {setup}"

            metadata = yaml.safe_load(mb_yml.read_text())
            url = metadata["metabase_url"]
            email, password = load_metabase_admin_credentials(project_root) or ("", "")
            assert password, "setup did not store the Metabase admin password"

            # A Metabase object that must survive every scenario below.
            marker = requests.post(
                f"{url}/api/collection",
                headers={"X-Metabase-Session": _session(url, email, password)},
                json={"name": "state-matrix-marker", "color": "#509EE3"},
                timeout=30,
            )
            assert marker.status_code == 200, marker.text

            def marker_exists(pw: str) -> bool:
                names = requests.get(
                    f"{url}/api/collection",
                    headers={"X-Metabase-Session": _session(url, email, pw)},
                    timeout=30,
                ).json()
                return any(c.get("name") == "state-matrix-marker" for c in names)

            def link_sso_copy(pw: str) -> None:
                """Make the linked Dango admin carry an SSO copy of ``pw`` (as a live project does)."""
                from dango.auth.database import update_user
                from dango.auth.metabase_sync import encrypt_metabase_password
                from dango.auth.models import UserUpdate

                db_path = get_auth_db_path(project_root)
                user = get_user_by_email(db_path, email)
                assert user is not None, "no Dango admin user for the Metabase admin email"
                update_user(
                    db_path,
                    user.id,
                    UserUpdate(metabase_password_enc=encrypt_metabase_password(pw, project_root)),
                )

            def make_stale_yaml() -> None:
                data = yaml.safe_load(mb_yml.read_text())
                data["admin"]["password"] = "stale-" + secrets.token_urlsafe(12)
                mb_yml.write_text(yaml.safe_dump(data, default_flow_style=False))
                MetabaseCredentialStore(project_root).delete()
                shutil.rmtree(project_root / ".dango" / "state", ignore_errors=True)

            # --- Scenario 1 (matrix row 2, beta-1's shape): stale YAML password, no protected
            # credential, SSO copy still valid -> adopted in place, Metabase untouched.
            link_sso_copy(password)
            make_stale_yaml()
            assert MetabaseCredentialStore(project_root).load() is None
            _stop(project_root, env)
            first = _dango_start(project_root, env)
            assert "Restoring Metabase admin access" not in first, first[-3000:]
            assert "migration is incomplete" not in first, first[-3000:]
            adopted = MetabaseCredentialStore(project_root).load()
            assert adopted == password, "SSO credential was not adopted into the protected store"
            assert "password" not in yaml.safe_load(mb_yml.read_text())["admin"]
            assert _login(url, email, adopted) == 200
            assert marker_exists(adopted)

            # --- Scenario 2 (matrix rows 3/4/14): every stored copy lost -> repaired by start.
            lost = secrets.token_urlsafe(24)
            headers = {"X-Metabase-Session": _session(url, email, adopted)}
            user_id = requests.get(f"{url}/api/user/current", headers=headers, timeout=30).json()[
                "id"
            ]
            changed = requests.put(
                f"{url}/api/user/{user_id}/password",
                headers=headers,
                json={"password": lost, "old_password": adopted},
                timeout=30,
            )
            assert changed.status_code == 200, changed.text
            make_stale_yaml()  # protected store + state gone, YAML stale; SSO copy is now stale too
            assert _login(url, email, adopted) != 200, "the old credential must be dead"
            _stop(project_root, env)
            second = _dango_start(project_root, env)
            assert "Metabase admin access restored." in second, second[-3000:]
            repaired = MetabaseCredentialStore(project_root).load()
            assert repaired and repaired not in {adopted, lost}
            assert _login(url, email, repaired) == 200
            assert "password" not in yaml.safe_load(mb_yml.read_text())["admin"]
            assert marker_exists(repaired), "Metabase data was lost during the repair"
            sso = get_user_by_email(get_auth_db_path(project_root), email)
            assert sso is not None and sso.metabase_password_enc

            # --- A further start has nothing to do: no repair, no warning.
            _stop(project_root, env)
            third = _dango_start(project_root, env)
            assert "Restoring Metabase admin access" not in third, third[-3000:]
            assert "migration is incomplete" not in third, third[-3000:]
            assert marker_exists(repaired)
        finally:
            keyring.set_keyring(previous_backend)
            _cleanup(project_root, env)
