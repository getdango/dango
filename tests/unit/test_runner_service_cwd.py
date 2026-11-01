"""tests/unit/test_runner_service_cwd.py

Service-user runner scripts start in the root directory on every path that renders them (setup,
upgrade, push and restore re-apply), because the SSH session's working directory is not readable by
the service user and GNU ``find`` fails when it cannot restore it.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.cloud import runtime_ownership as ro
from dango.platform.cloud import server_setup
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.runner_isolation_fakes import RunnerServer

pytestmark = pytest.mark.unit

WRAPPER = "cd / && runuser -u dango -- sh -c "
SERVICE_STEPS = [s.name for s in ri.build_steps() if s.run_as == "service"]
REAPPLY_SERVICE_STEPS = ["project_permissions", "notebooks_acl", "notebooks_report"]


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    backup = MagicMock()
    backup.archive_path = "/srv/dango/backups/deploy/b.tar.gz"
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield


def _settled() -> RunnerServer:
    srv = RunnerServer(version="1.0.14", marker=True, in_group=False)
    srv.record = json.dumps({"dango": "1.0.14", "docker_access": "helper"})
    srv.isolation_marker = json.dumps({"schema": 1, "loopback": "restricted"})
    return srv


def _step_commands(srv: RunnerServer) -> dict[str, str]:
    """Command each runner step ran as on the server (the script inside ``bash -c``), by step name."""
    found: dict[str, str] = {}
    for sent in srv.runner_commands:
        tag, _, body = shlex.split(sent)[2].partition("\n")
        found[tag.removeprefix("# runner-step:")] = body
    return found


def _assert_starts_in_root(srv: RunnerServer, expected: list[str]) -> None:
    commands = _step_commands(srv)
    for name in expected:
        assert name in commands, (name, sorted(commands))
        assert commands[name].startswith(WRAPPER), (name, commands[name][:80])


def test_wrapper_text_starts_in_the_root_directory() -> None:
    assert scripts.as_service_user("echo hi").startswith(WRAPPER)
    assert ri.Step("s", "echo hi", run_as="service").command == scripts.as_service_user("echo hi")


def test_setup_service_steps_start_in_the_root_directory() -> None:
    srv = RunnerServer(project=False, in_group=False)
    server_setup.setup_server(srv)
    _assert_starts_in_root(srv, SERVICE_STEPS)


def test_upgrade_service_steps_start_in_the_root_directory() -> None:
    srv = _settled()
    with patch.object(ro, "secure_runtime", return_value=None):
        upgrade_dango(srv, version="1.0.15")
    _assert_starts_in_root(srv, SERVICE_STEPS)


def test_push_reapply_service_steps_start_in_the_root_directory(tmp_path: Path) -> None:
    from dango.platform.cloud import deployer
    from dango.platform.cloud.file_sync import SyncResult

    srv = _settled()
    sync = SyncResult(synced_files=["scripts/"], dry_run=False)
    backup = MagicMock(warnings=[], archive_path="/b.tar.gz")
    with (
        patch("dango.platform.cloud.file_sync.sync_project_files", return_value=sync),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.pushed_requirements.install_pushed_requirements"),
    ):
        deployer.push_deploy(srv, tmp_path, "10.0.0.1")
    _assert_starts_in_root(srv, REAPPLY_SERVICE_STEPS)


def test_restore_reapply_service_steps_start_in_the_root_directory() -> None:
    from dango.platform.cloud import backup

    srv = _settled()
    with (
        patch("dango.platform.cloud.backup.extract_checked_archive"),
        patch("dango.platform.cloud.backup._remove_leftovers"),
    ):
        backup.restore_from_archive(srv, "/srv/dango/backups/deploy/a.tar.gz")
    _assert_starts_in_root(srv, REAPPLY_SERVICE_STEPS)


# --- behaviour: the rendered command really leaves the starting directory ---------------------------

#: Prints the working directory and lists it; listing an unreadable directory fails on every OS.
_PROBE = "pwd -P; ls . >/dev/null"

_FAKE_RUNUSER = """#!/bin/sh
[ "$1" = "-u" ] && shift 2
[ "$1" = "--" ] && shift
exec "$@"
"""

_LAUNCH = 'mkdir "$D" && cd "$D" && chmod 000 . && eval "$CMD"'


def _run_from_unreadable_dir(tmp_path: Path, command: str) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    fake = bindir / "runuser"
    fake.write_text(_FAKE_RUNUSER)
    fake.chmod(0o755)
    start = tmp_path / "start"
    start.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "D": str(start / "unreadable"),
        "CMD": command,
    }
    try:
        return subprocess.run(
            ["bash", "-c", _LAUNCH],
            cwd=start,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        (start / "unreadable").chmod(0o755)


def test_rendered_command_runs_from_root_even_when_the_starting_directory_is_unreadable(
    tmp_path: Path,
) -> None:
    proc = _run_from_unreadable_dir(tmp_path / "new", scripts.as_service_user(_PROBE))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "/"


def test_positive_control_the_unwrapped_command_stays_in_the_starting_directory(
    tmp_path: Path,
) -> None:
    old = f"runuser -u dango -- sh -c {shlex.quote(_PROBE)}"
    proc = _run_from_unreadable_dir(tmp_path / "old", old)
    assert proc.stdout.strip() != "/"
    assert "unreadable" in proc.stdout or proc.returncode != 0


def test_reapply_step_commands_all_parse_as_one_shell_command() -> None:
    from dango.platform.cloud import runner_reapply

    for step in runner_reapply.reapply_steps():
        if step.run_as == "service":
            parts = shlex.split(step.command)
            assert parts[:7] == ["cd", "/", "&&", "runuser", "-u", "dango", "--"], step.name
            assert parts[7:9] == ["sh", "-c"], step.name
            assert len(parts) == 10, step.name
