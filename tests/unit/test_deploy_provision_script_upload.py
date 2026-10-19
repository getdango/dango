"""tests/unit/test_deploy_provision_script_upload.py

Remote Python is uploaded as a file (not passed as exec(base64)); HTTPS health checks verify TLS.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from dango.cli.commands import deploy_provision
from dango.cli.commands.deploy_provision import _create_admin_and_enable_auth, _health_check


def _ssh(fail_run: bool = False) -> MagicMock:
    ssh = MagicMock()

    def run(cmd: str, **_kw: object) -> MagicMock:
        failing = fail_run and cmd.startswith("sudo -u dango")
        stdout = "/tmp/dango-x.AbCd1234" if cmd.startswith("mktemp") else ""
        return MagicMock(exit_code=1 if failing else 0, stdout=stdout, stderr="err")

    ssh.exec_command.side_effect = run
    return ssh


def _commands(ssh: MagicMock) -> list[str]:
    return [c.args[0] for c in ssh.exec_command.call_args_list]


@pytest.mark.unit
def test_remote_script_uploaded_not_exec_base64() -> None:
    ssh = _ssh()
    with (
        patch.object(deploy_provision, "write_project_file"),
        patch.object(
            deploy_provision, "append_env_line_if_missing", return_value=MagicMock(exit_code=0)
        ),
    ):
        _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")

    for cmd in _commands(ssh):
        assert "base64" not in cmd
        assert "exec(" not in cmd
    writes = ssh.write_remote_file.call_args_list
    assert len(writes) == 2
    for call in writes:
        path = call.args[0]
        # Inside a directory the server made; mode leaves it unreadable to others.
        assert path.startswith("/tmp/dango-x.AbCd1234/")
        assert call.kwargs["mode"] == 0o640
        assert any(c.startswith("sudo -u dango") and path in c for c in _commands(ssh))
        assert "rm -rf /tmp/dango-x.AbCd1234" in _commands(ssh)


@pytest.mark.unit
def test_remote_script_removed_even_on_failure() -> None:
    ssh = _ssh(fail_run=True)
    result = deploy_provision._run_remote_script(ssh, "print(1)", purpose="t", timeout=5)
    assert result.exit_code == 1
    assert "rm -rf /tmp/dango-x.AbCd1234" in _commands(ssh)

    ssh = _ssh()
    ssh.write_remote_file.side_effect = OSError("sftp down")
    with pytest.raises(OSError):
        deploy_provision._run_remote_script(ssh, "print(1)", purpose="t", timeout=5)
    assert "rm -rf /tmp/dango-x.AbCd1234" in _commands(ssh)


@pytest.mark.unit
def test_health_check_verifies_tls_for_https() -> None:
    ok = MagicMock(status_code=200)
    with patch("httpx.get", return_value=ok) as get:
        assert _health_check("https://example.com", timeout=3, interval=1) is True
    assert get.call_args.kwargs.get("verify", True) is True


@pytest.mark.unit
def test_health_check_reports_tls_not_ready() -> None:
    with (
        patch("httpx.get", side_effect=httpx.ConnectError("certificate verify failed")),
        patch.object(deploy_provision.time, "sleep"),
        patch.object(deploy_provision.console, "print") as out,
    ):
        assert _health_check("https://example.com", timeout=3, interval=1) is False
    assert "HTTPS is not ready yet" in out.call_args.args[0]
