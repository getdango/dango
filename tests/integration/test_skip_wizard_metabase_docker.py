"""tests/integration/test_skip_wizard_metabase_docker.py

Real-Docker check that `dango init --skip-wizard` with no DANGO_ADMIN_EMAIL ends with a
configured, SSO-linked Metabase (C9), that a legacy admin@localhost project is reported
honestly, and what happens when the default admin is replaced and deleted.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

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


def _docker_names(cmd: list[str]) -> list[str]:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    return [line for line in out.stdout.splitlines() if line.strip()]


def _leftovers(compose_project_name: str) -> list[str]:
    """Volumes/images/containers still carrying this run's compose project name."""
    found = _docker_names(
        [
            "docker",
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
            "--filter",
            f"name={compose_project_name}",
        ]
    )
    found += _docker_names(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
    found = [n for n in found if compose_project_name in n]
    found += _docker_names(
        [
            "docker",
            "ps",
            "-a",
            "--format",
            "{{.Names}}",
            "--filter",
            f"label=com.docker.compose.project={compose_project_name}",
        ]
    )
    return found


def _cleanup(
    project_root: Path, compose_project_name: str, isolated_env: dict[str, str]
) -> list[str]:
    """Stop the project (isolated HOME), then tear Docker down with the REAL environment.

    The compose plugin does not resolve under a fake HOME, which silently leaked the volume;
    ``down -v`` also never removes the built image, hence ``--rmi local``.
    """
    subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "stop"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=180,
        env=isolated_env,
        stdin=subprocess.DEVNULL,
    )
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
    assert down.returncode == 0, f"docker compose down failed: {down.stderr[-2000:]}"
    return _leftovers(compose_project_name)


def _run_dango(
    project_root: Path,
    env: dict[str, str],
    *args: str,
    input_text: str | None = None,
    timeout: int = 900,
) -> tuple[int, str]:
    """Run the real CLI as a subprocess and return (exit code, ANSI-stripped output)."""
    result = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        input=input_text,
        stdin=None if input_text is not None else subprocess.DEVNULL,
    )
    return result.returncode, _ANSI_RE.sub("", result.stdout + result.stderr)


def _dango_start(project_root: Path, env: dict[str, str]) -> str:
    code, output = _run_dango(project_root, env, "start", "-y")
    assert code == 0, f"dango start failed ({code}):\n{output[-3000:]}"
    return output


def _wait_healthy(base_url: str) -> None:
    import requests

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if requests.get(f"{base_url}/api/health", timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    pytest.fail(f"Dango web server never became healthy at {base_url}")


def _dango_login(base_url: str, email: str, password: str):  # type: ignore[no-untyped-def]
    import requests

    session = requests.Session()
    response = session.post(
        f"{base_url}/api/auth/login",
        json={"email": email, "password": password},
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=15,
    )
    return session, response


def _metabase_login(url: str, email: str, password: str) -> int:
    import requests

    return requests.post(
        f"{url}/api/session", json={"username": email, "password": password}, timeout=30
    ).status_code


@dataclass
class _Project:
    root: Path
    env: dict[str, str]
    compose_project_name: str
    base_url: str


@pytest.fixture
def docker_project(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[_Project]:
    """A --skip-wizard scratch project in an isolated HOME; torn down with the real Docker env.

    ``request.param`` (optional, via indirect parametrisation) is the DANGO_ADMIN_EMAIL to
    export at ``init`` time; the default is to export none.
    """
    if os.environ.get("DANGO_RUN_DOCKER_TESTS") != "1":
        pytest.skip("Set DANGO_RUN_DOCKER_TESTS=1 to run (heavyweight, needs Docker)")
    if shutil.which("docker") is None:
        pytest.skip("Docker not available")

    import keyring
    from keyring.backends.fail import Keyring as NoKeyring

    from dango.cli.init import init_project
    from dango.config import ConfigLoader
    from dango.platform.docker import get_compose_project_name
    from dango.security import metabase_credentials

    project_root = tmp_path_factory.mktemp("skip-wizard-metabase")
    fake_home = tmp_path_factory.mktemp("skip-wizard-metabase-home")
    previous_backend = keyring.get_keyring()
    keyring.set_keyring(NoKeyring())
    monkeypatch.setattr(
        metabase_credentials,
        "_LOCAL_SECRETS_DIR",
        fake_home / ".dango" / "secrets" / "metabase",
    )
    # The escape: the old harnesses exported DANGO_ADMIN_EMAIL here. Do not.
    monkeypatch.delenv("DANGO_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("DANGO_ADMIN_PASSWORD", raising=False)
    init_email = getattr(request, "param", None)
    if init_email:
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", init_email)

    env = {
        **{k: v for k, v in os.environ.items() if k != "DANGO_ADMIN_EMAIL"},
        "HOME": str(fake_home),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "BROWSER": "true",
        "DANGO_LOG_LEVEL": "ERROR",
    }
    leftovers: list[str] = []
    compose_project_name = ""
    try:
        init_project(project_root, skip_wizard=True)
        monkeypatch.delenv("DANGO_ADMIN_EMAIL", raising=False)
        _configure_unique_docker_ports(project_root)
        # Computed BEFORE Docker starts so teardown works even if start fails midway.
        compose_project_name = get_compose_project_name(project_root)
        config = ConfigLoader(project_root).load_config()
        yield _Project(
            root=project_root,
            env=env,
            compose_project_name=compose_project_name,
            base_url=f"http://127.0.0.1:{config.platform.port}",
        )
    finally:
        try:
            if compose_project_name:
                leftovers = _cleanup(project_root, compose_project_name, env)
        finally:
            keyring.set_keyring(previous_backend)
    assert not leftovers, f"Docker resources leaked after teardown: {leftovers}"


def _metabase_url(project_root: Path) -> str:
    import yaml

    data = yaml.safe_load((project_root / ".dango" / "metabase.yml").read_text())
    return str(data["metabase_url"])


@pytest.mark.integration
def test_skip_wizard_init_start_configures_metabase(docker_project: _Project) -> None:
    """init --skip-wizard (no email env) -> start leaves Metabase configured and SSO-linked."""
    root = docker_project.root
    output = _dango_start(root, docker_project.env)

    assert (root / ".dango" / "metabase.yml").exists(), output[-3000:]
    assert "Metabase configured automatically" in output, output[-3000:]
    assert "Metabase was not set up" not in output, output[-3000:]
    from dango.auth.admin import SKIP_WIZARD_DEFAULT_ADMIN_EMAIL, get_auth_db_path
    from dango.auth.database import get_user_by_email
    from dango.security.metabase_config import load_metabase_admin_credentials

    creds = load_metabase_admin_credentials(root)
    assert creds, "no Metabase admin credentials stored"
    assert _metabase_login(_metabase_url(root), *creds) == 200
    admin = get_user_by_email(get_auth_db_path(root), SKIP_WIZARD_DEFAULT_ADMIN_EMAIL)
    assert admin is not None
    assert admin.metabase_user_id is not None, "Dango admin was not SSO-linked into Metabase"


@pytest.mark.integration
@pytest.mark.parametrize("docker_project", ["admin@localhost"], indirect=True)
def test_legacy_admin_localhost_project_is_reported_honestly(docker_project: _Project) -> None:
    """A project whose admin is admin@localhost is told Metabase was skipped, not configured."""
    root = docker_project.root
    code, output = _run_dango(root, docker_project.env, "start", "-y")
    assert code == 0, output[-3000:]
    assert "Metabase was not set up" in output, output[-3000:]
    assert "Metabase configured automatically" not in output, output[-3000:]
    assert not (root / ".dango" / "metabase.yml").exists()


@pytest.mark.integration
def test_replacing_default_admin_keeps_metabase_and_sso_working(docker_project: _Project) -> None:
    """Matrix row 13: add a real admin, delete admin@dango.test, check Metabase + SSO survive."""
    from dango.security.metabase_config import load_metabase_admin_credentials

    root, env, base_url = docker_project.root, docker_project.env, docker_project.base_url
    _dango_start(root, env)
    _wait_healthy(base_url)
    url = _metabase_url(root)
    creds_before = load_metabase_admin_credentials(root)
    yml_before = (root / ".dango" / "metabase.yml").read_text()
    assert creds_before and _metabase_login(url, *creds_before) == 200

    new_email = "second@dango-t3.test"
    code, added = _run_dango(
        root, env, "auth", "add-user", new_email, "--role", "admin", "--password"
    )
    assert code == 0, added
    match = re.search(r"Password:\s+(\S+)", added)
    assert match, added
    new_password = match.group(1)

    _, login = _dango_login(base_url, new_email, new_password)
    assert login.status_code == 200, f"second admin login failed: {login.status_code} {login.text}"

    code, deleted = _run_dango(
        root, env, "auth", "delete-user", "admin@dango.test", input_text="yes\n", timeout=300
    )
    report = [f"(1) delete-user exit={code} cleanup_failed={'Metabase cleanup failed' in deleted}"]
    mb_login_after = _metabase_login(url, *creds_before)
    report.append(f"(2) metabase admin credential login after delete: {mb_login_after}")
    session2, _ = _dango_login(base_url, new_email, new_password)
    proxied = session2.get(f"{base_url}/metabase/api/user/current", timeout=30)
    report.append(f"(3) second admin via proxy /metabase/api/user/current: {proxied.status_code}")
    code4, again = _run_dango(root, env, "start", "-y")
    report.append(
        f"(4) restart exit={code4} repair={'Restoring Metabase admin access' in again} "
        f"skipped={'Metabase was not set up' in again}"
    )
    yml_same = (root / ".dango" / "metabase.yml").read_text() == yml_before
    report.append(
        f"yml unchanged: {yml_same}; creds unchanged: "
        f"{load_metabase_admin_credentials(root) == creds_before}"
    )
    summary = "\n".join(report) + f"\n--- delete output ---\n{deleted[-1500:]}"
    print(summary)  # reported item by item; visible with `pytest -s` / on failure
    assert code == 0, summary
    assert "deleted." in deleted, summary
    assert mb_login_after == 200, summary
    assert proxied.status_code == 200, summary
    assert code4 == 0 and "Restoring Metabase admin access" not in again, summary
    assert "Metabase was not set up" not in again, summary
