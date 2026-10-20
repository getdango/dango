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
        stdout = (
            "/tmp/" + cmd.split()[-1].replace("XXXXXXXX", "AbCd1234") if "mktemp -d" in cmd else ""
        )
        return MagicMock(exit_code=1 if failing else 0, stdout=stdout, stderr="err")

    ssh.exec_command.side_effect = run
    return ssh


def _commands(ssh: MagicMock) -> list[str]:
    return [c.args[0] for c in ssh.exec_command.call_args_list]


_DIR = "/tmp/dango-x.AbCd1234"


def _ssh_with(overrides: dict[str, tuple[int, str, str]]) -> MagicMock:
    """Fake ssh: a command starting with a key in *overrides* returns (exit, stdout, stderr)."""
    ssh = MagicMock()

    def run(cmd: str, **_kw: object) -> MagicMock:
        for prefix, (code, out, err) in overrides.items():
            if cmd.startswith(prefix):
                return MagicMock(exit_code=code, stdout=out, stderr=err)
        out = _DIR if cmd.startswith(("mktemp", "env -u TMPDIR mktemp")) else ""
        return MagicMock(exit_code=0, stdout=out, stderr="")

    ssh.exec_command.side_effect = run
    return ssh


def _index(cmds: list[str], prefix: str) -> int:
    return next(i for i, c in enumerate(cmds) if prefix in c)


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
        assert path.startswith("/tmp/dango-") and ".AbCd1234/" in path
        assert call.kwargs["mode"] == 0o640
        assert any(c.startswith("sudo -u dango") and path in c for c in _commands(ssh))
        assert f"rm -rf {path.rsplit('/', 1)[0]}" in _commands(ssh)


@pytest.mark.unit
def test_remote_script_removed_even_on_failure() -> None:
    ssh = _ssh(fail_run=True)
    result = deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
    assert result.exit_code == 1
    assert "rm -rf /tmp/dango-x.AbCd1234" in _commands(ssh)

    ssh = _ssh()
    ssh.write_remote_file.side_effect = OSError("sftp down")
    with pytest.raises(OSError):
        deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
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


@pytest.mark.unit
@pytest.mark.parametrize("stdout", ["", "relative/dir", "two words", f"{_DIR}\n/tmp/other", "\n"])
def test_unexpected_mktemp_output_fails_and_never_runs_script(stdout: str) -> None:
    ssh = _ssh_with({"env -u TMPDIR mktemp": (0, stdout, "")})
    result = deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
    assert result.exit_code != 0
    assert "unexpected" in result.stderr
    ssh.write_remote_file.assert_not_called()
    assert not any(c.startswith("sudo -u dango") for c in _commands(ssh))


@pytest.mark.unit
def test_unexpected_mktemp_output_removes_the_printed_directory() -> None:
    ssh = _ssh_with({"env -u TMPDIR mktemp": (0, f"{_DIR}\nextra line", "")})
    result = deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
    assert result.exit_code != 0
    assert f"rm -rf {_DIR}" in _commands(ssh)


@pytest.mark.unit
def test_admin_creation_fails_when_script_cannot_be_staged() -> None:
    ssh = _ssh_with({"env -u TMPDIR mktemp": (0, "", "")})
    with (
        patch.object(deploy_provision, "write_project_file") as write,
        pytest.raises(deploy_provision.AdminCreationError),
    ):
        _create_admin_and_enable_auth(ssh, "admin@test.com", "password123")
    write.assert_not_called()  # auth is never enabled without an admin


@pytest.mark.unit
def test_mktemp_ignores_root_tmpdir_and_directory_is_verified() -> None:
    ssh = _ssh_with({})
    deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
    cmds = _commands(ssh)
    assert cmds[0].startswith("env -u TMPDIR mktemp -d")
    check = next(c for c in cmds if "stat -c" in c)
    assert "! -L" in check and "0:700" in check


@pytest.mark.unit
def test_steps_run_in_order() -> None:
    ssh = _ssh_with({})
    order: list[str] = []
    ssh.write_remote_file.side_effect = lambda *a, **k: order.append("write")
    real = ssh.exec_command.side_effect
    ssh.exec_command.side_effect = lambda c, **k: (order.append(c.split()[0]), real(c, **k))[1]
    deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
    cmds = _commands(ssh)
    chmod_at = _index(cmds, "chmod 750")
    grant_at = _index(cmds, "chgrp -h dango")
    run_at = _index(cmds, "sudo -u dango")
    assert chmod_at < grant_at < run_at
    # the directory is prepared before the upload, the file is granted after it
    assert order.index("write") > order.index("chgrp")


@pytest.mark.unit
@pytest.mark.parametrize("failing", ["test ! -L", "chgrp dango", "chgrp -h dango"])
def test_failing_prep_step_skips_run_and_cleans_up(failing: str) -> None:
    ssh = _ssh_with({failing: (1, "", "boom")})
    result = deploy_provision._run_remote_script(ssh, "print(1)", purpose="x", timeout=5)
    assert result.exit_code == 1
    assert not any(c.startswith("sudo -u dango") for c in _commands(ssh))
    assert f"rm -rf {_DIR}" in _commands(ssh)
