"""tests/factories/cloud_factories.py

Shared mock factories for cloud/SSH test modules.

Usage:
    from tests.factories.cloud_factories import make_ssh_mock, make_ssh_mock_configurable

    ssh = make_ssh_mock()  # simple: all commands succeed
    ssh = make_ssh_mock_configurable(exec_results={
        "systemctl": ("active", "", 0),
    })
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from dango.platform.cloud.ssh import CommandResult

#: Host interpreter the loose fakes report (Ubuntu 24.04 style); real detection is tested in
#: test_server_python.py.
HOST_PYTHON = "/usr/bin/python3.12"


@contextmanager
def host_python_server() -> Iterator[None]:
    """Loose SSH mocks: the server has a root-owned Python 3.12 and the venv runs on it."""
    with (
        patch("dango.platform.cloud.server_python.host_probe", return_value=(HOST_PYTHON, None)),
        patch("dango.platform.cloud.server_python.venv_python_in_range", return_value=True),
    ):
        yield


def make_ssh_mock() -> MagicMock:
    """Return a mock SSHManager where all commands succeed.

    Used by: test_deployer, test_deployer_push, test_deploy_journal,
    test_remote_backup_cli, test_remote_ops_cli.
    """
    ssh = MagicMock()
    ssh.key_path = Path("/tmp/test_key")
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    return ssh


def make_ssh_mock_configurable(
    *,
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> MagicMock:
    """Return a mock SSHManager with configurable exec_command results.

    Args:
        exec_results: Map of command substring -> (stdout, stderr, exit_code).
            If a command matches multiple substrings, the first match wins.
            Commands not matched default to success ("", "", 0).

    Used by: test_backup, test_server_setup.
    """
    results = exec_results or {}

    def _exec_side_effect(command: str, **kwargs: object) -> CommandResult:
        if command.startswith('[ -n "$(find ') and "-user root" in command:
            # The compose-helper marker probe: absent unless a test says otherwise.
            for substr, (stdout, stderr, exit_code) in results.items():
                if substr in command:
                    return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
            return CommandResult(stdout="", stderr="", exit_code=1)
        for substr, (stdout, stderr, exit_code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        if 'command -v "$n"' in command:  # host interpreter detection
            return CommandResult(stdout=HOST_PYTHON + "\n", stderr="", exit_code=0)
        if command.startswith("cat ") and command.endswith("/manifest.json"):
            return CommandResult(stdout="{}", stderr="", exit_code=0)  # lists nothing to check
        if command.startswith("test -f ") and "/metabase/metabase.db" in command:
            return CommandResult(
                stdout="", stderr="", exit_code=1
            )  # no Metabase data in the archive
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    return ssh
