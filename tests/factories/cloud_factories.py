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

from pathlib import Path
from unittest.mock import MagicMock

from dango.platform.cloud.ssh import CommandResult


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
        for substr, (stdout, stderr, exit_code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    return ssh


def make_httpx_response(status_code: int, json_data: object = None) -> MagicMock:
    """Return a mock httpx.Response."""
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data or {}
    response.raise_for_status = MagicMock()
    if status_code >= 400:
        from httpx import HTTPStatusError

        response.raise_for_status.side_effect = HTTPStatusError(
            message=f"HTTP {status_code}",
            request=MagicMock(),
            response=response,
        )
    return response
