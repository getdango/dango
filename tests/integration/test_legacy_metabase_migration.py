"""tests/integration/test_legacy_metabase_migration.py

Release-only real-Docker check: a project that still has a legacy plaintext Metabase admin
password migrates it on the first real `dango start` (1.0.10 shipped with the migration
running before Metabase was ready, so it never completed on upgraded projects).
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

_ADMIN_EMAIL = "admin@dango-legacy-migration.test"
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
    from tests.integration.docker_leak_support import (
        assert_no_docker_leftovers,
        compose_down_and_prune,
    )

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
    compose_down_and_prune(project_root, name)
    assert_no_docker_leftovers(name)


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


@pytest.mark.integration
class TestLegacyMetabaseCredentialMigration:
    """A legacy project's Metabase admin password is migrated by the first `dango start`."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_docker(self) -> None:
        if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
            pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
        if shutil.which("docker") is None:
            pytest.skip("Docker not available")

    def test_first_start_migrates_legacy_password_and_second_start_is_quiet(
        self, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import keyring
        import requests
        import yaml
        from keyring.backends.fail import Keyring as NoKeyring

        from dango.cli.init import init_project
        from dango.config import ConfigLoader
        from dango.platform import DockerManager
        from dango.platform.common.metabase_credential_migration import migration_pending
        from dango.platform.common.startup import setup_metabase_if_needed, start_docker_services
        from dango.security import metabase_credentials
        from dango.security.metabase_config import load_metabase_admin_credentials
        from dango.security.metabase_credentials import MetabaseCredentialStore

        project_root = tmp_path_factory.mktemp("legacy-metabase-migration")
        fake_home = tmp_path_factory.mktemp("legacy-metabase-home")

        # Never touch the developer's keychain or ~/.dango: force the owner-only file fallback
        # under a throwaway HOME, both in this process and in the `dango start` subprocesses.
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
        try:
            init_project(project_root, skip_wizard=True)
            _configure_unique_docker_ports(project_root)
            config = ConfigLoader(project_root).load_config()

            # 1. A normal 1.0.10-style project: real Metabase, protected credential.
            docker_manager = DockerManager(project_root)
            start_docker_services(project_root)
            setup = setup_metabase_if_needed(project_root, config.project.name, organization=None)
            assert setup.get("success"), f"Metabase setup failed: {setup}"

            # 2. Turn it into a pre-1.0.10 project: password back in metabase.yml, nothing in
            #    the protected store, no migration state.
            store = MetabaseCredentialStore(project_root)
            password = store.load()
            assert password, "setup did not store the Metabase admin password"
            mb_yml = project_root / ".dango" / "metabase.yml"
            metadata = yaml.safe_load(mb_yml.read_text())
            metadata["admin"]["password"] = password
            mb_yml.write_text(yaml.safe_dump(metadata, default_flow_style=False))
            store.delete()
            shutil.rmtree(project_root / ".dango" / "state", ignore_errors=True)
            assert migration_pending(project_root), "fixture did not produce a legacy project"
            assert MetabaseCredentialStore(project_root).load() is None

            # 3. Cold start through the real CLI: containers are (re)started and Metabase is
            #    still booting when the credential migration runs.
            docker_manager.stop_services()
            first = _dango_start(project_root, env)
            assert "migration is incomplete" not in first, first[-3000:]

            after = yaml.safe_load(mb_yml.read_text())
            assert "password" not in after.get("admin", {}), "legacy password still in metabase.yml"
            migrated = MetabaseCredentialStore(project_root).load()
            assert migrated, "protected store has no credential after the first start"

            # 4. The migrated credential really works against Metabase.
            credentials = load_metabase_admin_credentials(project_root)
            assert credentials is not None
            login = requests.post(
                f"{after['metabase_url']}/api/session",
                json={"username": credentials[0], "password": credentials[1]},
                timeout=30,
            )
            assert login.status_code == 200, login.text

            # 5. A second start has nothing pending: no warning and no extra wait.
            subprocess.run(
                [sys.executable, "-m", "dango.cli.main", "stop"],
                cwd=project_root,
                capture_output=True,
                timeout=180,
                env=env,
                stdin=subprocess.DEVNULL,
            )
            second = _dango_start(project_root, env)
            assert "migration is incomplete" not in second, second[-3000:]
            assert "Waiting for Metabase to be ready" not in second, second[-3000:]
        finally:
            keyring.set_keyring(previous_backend)
            _cleanup(project_root, env)
