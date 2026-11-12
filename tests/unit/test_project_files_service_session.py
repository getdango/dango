"""tests/unit/test_project_files_service_session.py

1.0.14-G5F1: ``dango remote env ...`` connects over SSH as the ``dango`` service user,
which may not call ``runuser`` ("runuser: may not be used by non-root users").  The
``project_files`` helpers must run the same scripts directly in a ``dango`` session and
stay byte-identical (``runuser -u dango --``) in a root session.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands import remote_env
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import project_files as pf
from dango.platform.cloud.project_files import (
    append_env_line_if_missing,
    read_project_file,
    write_project_file,
)

_RUNUSER_PREFIX = "runuser -u dango -- "
_READ_ARGS = 'sh -c \'[ -e "$1" -o -L "$1" ] && exec cat "$1"; exit 3\' _ /srv/dango/project/.env'


class RecordingSSH:
    """Fake SSH session that records commands and files; no shell involved."""

    def __init__(self, username: Any, stdout: str = "", exit_code: int = 0) -> None:
        self.username = username
        self.commands: list[str] = []
        self.written: list[tuple[str, Any, int]] = []
        self.uploaded: list[tuple[Path, str]] = []
        self._stdout = stdout
        self._exit = exit_code

    def exec_command(self, command: str, timeout: int | None = None, check: bool = False) -> Any:
        self.commands.append(command)
        out = "/tmp/dango-stage.AbC123\n" if command == "mktemp -d" else self._stdout
        return SimpleNamespace(stdout=out, stderr="", exit_code=self._exit, success=self._exit == 0)

    def write_remote_file(self, path: str, content: Any, mode: int = 0o644) -> None:
        self.written.append((path, content, mode))

    def upload_file(self, local: Path, remote: str) -> None:
        self.uploaded.append((local, remote))


def _run_all_helpers(ssh: RecordingSSH) -> None:
    read_project_file(ssh, "/srv/dango/project/.env")
    write_project_file(ssh, "/srv/dango/project/.env", "A=1\n", mode=0o600)
    append_env_line_if_missing(ssh, "/srv/dango/project/.env", "K", "v")


@pytest.mark.unit
class TestServiceUserSessionEmitsNoRunuser:
    def test_no_command_contains_runuser(self):
        ssh = RecordingSSH("dango")
        _run_all_helpers(ssh)
        assert ssh.commands
        assert not [c for c in ssh.commands if "runuser" in c], ssh.commands

    def test_staging_is_not_under_root_home(self):
        ssh = RecordingSSH("dango")
        write_project_file(ssh, "/srv/dango/project/.env", "A=1\n", mode=0o600)
        assert ssh.written
        assert not any(p.startswith("/root") for p, _c, _m in ssh.written)
        assert not any("/root" in c for c in ssh.commands)
        assert ssh.commands[0] == "mktemp -d"

    def test_staged_file_is_private_and_cleaned_up(self):
        ssh = RecordingSSH("dango")
        write_project_file(ssh, "/srv/dango/project/.env", "A=1\n", mode=0o600)
        (staged, content, mode) = ssh.written[0]
        assert content == "A=1\n"
        assert mode == 0o600
        final = ssh.commands[-1]
        assert shlex.quote(staged) in final or staged in final
        assert "rm -rf" in final or "rm -f" in final

    def test_source_upload_goes_to_private_staging(self, tmp_path):
        src = tmp_path / "driver.jar"
        src.write_bytes(b"jar")
        ssh = RecordingSSH("dango")
        write_project_file(ssh, "/srv/dango/project/x.jar", "", mode=0o644, source=src)
        assert ssh.uploaded and not ssh.uploaded[0][1].startswith("/root")
        assert not [c for c in ssh.commands if "runuser" in c]

    def test_failed_write_raises(self):
        ssh = RecordingSSH("dango", exit_code=1)
        with pytest.raises(CloudProvisioningError):
            write_project_file(ssh, "/srv/dango/project/.env", "A=1\n", mode=0o600)

    def test_read_absent_is_empty_and_other_failure_raises(self):
        assert read_project_file(RecordingSSH("dango", exit_code=3), "/p/.env") == ""
        with pytest.raises(CloudProvisioningError):
            read_project_file(RecordingSSH("dango", exit_code=1), "/p/.env")

    def test_read_runs_script_directly(self):
        ssh = RecordingSSH("dango", stdout="A=1\n")
        assert read_project_file(ssh, "/srv/dango/project/.env") == "A=1\n"
        assert ssh.commands == [_READ_ARGS]


@pytest.mark.unit
class TestRootSessionIsUnchanged:
    """Golden text for the root path: deploy code runs these as root."""

    def test_read_golden(self):
        ssh = RecordingSSH("root", stdout="A=1\n")
        read_project_file(ssh, "/srv/dango/project/.env")
        assert ssh.commands == [_RUNUSER_PREFIX + _READ_ARGS]

    def test_append_golden(self):
        ssh = RecordingSSH("root")
        append_env_line_if_missing(ssh, "/srv/dango/project/.env", "K", "v w")
        script = 'grep -q "^$2=" "$1" 2>/dev/null || printf "%s\\n" "$3" >> "$1"'
        assert ssh.commands == [
            f"{_RUNUSER_PREFIX}sh -c {shlex.quote(script)} _ /srv/dango/project/.env K "
            f"{shlex.quote('K=v w')}"
        ]

    def test_write_golden(self):
        ssh = RecordingSSH("root")
        write_project_file(ssh, "/srv/dango/project/.env", "A=1\n", mode=0o600)
        (staging, content, mode) = ssh.written[0]
        assert re.fullmatch(r"/root/\.dango-upload-[0-9a-f]{16}", staging)
        assert (content, mode) == ("A=1\n", 0o600)
        assert ssh.commands == [
            f"{_RUNUSER_PREFIX}sh -c {shlex.quote(pf._WRITE_SCRIPT)} _ "
            f"/srv/dango/project/.env 600 < {staging}; rc=$?; rm -f {staging}; exit $rc"
        ]

    def test_write_source_golden(self, tmp_path):
        src = tmp_path / "d.jar"
        src.write_bytes(b"x")
        ssh = RecordingSSH("root")
        write_project_file(ssh, "/srv/dango/project/d.jar", "", mode=0o644, source=src)
        staging = ssh.uploaded[0][1]
        assert re.fullmatch(r"/root/\.dango-upload-[0-9a-f]{16}", staging)
        assert ssh.commands[0] == f"chmod 600 {staging}"
        assert ssh.commands[1].startswith(_RUNUSER_PREFIX + "sh -c ")

    @pytest.mark.parametrize("username", ["root", "ubuntu", None, ""])
    def test_any_identity_but_dango_keeps_runuser(self, username):
        ssh = RecordingSSH("root")
        ssh.username = username
        _run_all_helpers(ssh)
        assert all(_RUNUSER_PREFIX in c for c in ssh.commands if "chmod 600" not in c)


# ---------------------------------------------------------------------------
# `dango remote env ...` through a real shell, for both identities
# ---------------------------------------------------------------------------


class ShellSSH:
    """Fake SSH session that runs each command in a real local ``sh``.

    ``runuser -u dango --`` (root sessions only) is emulated by dropping the prefix,
    because the test user is the only user.  A real ``runuser`` call fails exactly as
    the server does for a non-root session: the fake puts a ``runuser`` on PATH that
    refuses to run, and the root session maps it to a pass-through.
    """

    def __init__(self, username: str, root_dir: Path, bin_dir: Path) -> None:
        self.issued: list[str] = []
        self.username = username
        self.root_dir = root_dir
        self.bin_dir = bin_dir
        self.disconnected = False

    def _map(self, text: str) -> str:
        return text.replace("/root/", f"{self.root_dir}/")

    def exec_command(self, command: str, timeout: int | None = None, check: bool = False) -> Any:
        self.issued.append(command)
        env = {
            **os.environ,
            "PATH": f"{self.bin_dir}{os.pathsep}{os.environ['PATH']}",
            "TMPDIR": str(self.root_dir),
        }
        proc = subprocess.run(
            ["sh", "-c", self._map(command)],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        return SimpleNamespace(
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
            success=proc.returncode == 0,
        )

    def write_remote_file(self, path: str, content: Any, mode: int = 0o644) -> None:
        target = Path(self._map(path))
        data = content.encode() if isinstance(content, str) else content
        target.write_bytes(data)
        target.chmod(mode)

    def upload_file(self, local: Path, remote: str) -> None:
        shutil.copyfile(local, self._map(remote))

    def disconnect(self) -> None:
        self.disconnected = True


def _install_shims(bin_dir: Path, *, root_session: bool) -> None:
    """Shim ``runuser`` (as the server behaves) and GNU ``mv -fT`` (BSD ``mv`` lacks -T)."""
    bin_dir.mkdir()
    runuser = bin_dir / "runuser"
    if root_session:
        runuser.write_text('#!/bin/sh\nshift 3\nexec "$@"\n')
    else:
        runuser.write_text(
            '#!/bin/sh\necho "runuser: may not be used by non-root users" >&2\nexit 1\n'
        )
    runuser.chmod(0o755)
    mv = bin_dir / "mv"
    real_mv = shutil.which("mv")
    mv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-fT" ]; then shift; exec ' + str(real_mv) + ' -f "$@"; fi\n'
        "exec " + str(real_mv) + ' "$@"\n'
    )
    mv.chmod(0o755)


@pytest.fixture(params=["dango", "root"])
def session(request, tmp_path):
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    bin_dir = tmp_path / "bin"
    _install_shims(bin_dir, root_session=request.param == "root")
    ssh = ShellSSH(request.param, root_dir, bin_dir)
    env_path = project / ".env"
    with (
        patch.object(remote_env, "_REMOTE_ENV_PATH", str(env_path)),
        patch(
            "dango.cli.commands.remote._ssh_connect_or_fail",
            return_value=(SimpleNamespace(), ssh, tmp_path),
        ),
    ):
        yield ssh, env_path


def _invoke(*args: str) -> Any:
    result = CliRunner().invoke(remote_env.env, list(args), catch_exceptions=False)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    return SimpleNamespace(exit_code=result.exit_code, output=plain)


@pytest.mark.unit
class TestRemoteEnvCommandsInBothSessions:
    def test_set_get_list_delete(self, session):
        ssh, env_path = session
        out = _invoke("set", "FOO=bar")
        assert out.exit_code == 0, out.output
        assert env_path.read_text() == "FOO=bar\n"
        assert oct(env_path.stat().st_mode & 0o777) == "0o600"

        assert _invoke("set", "BAZ=qux").exit_code == 0
        assert "FOO=***" in _invoke("get", "FOO").output
        listed = _invoke("list").output
        assert "FOO=***" in listed and "BAZ=***" in listed and "2 variable" in listed

        assert _invoke("delete", "FOO").exit_code == 0
        assert env_path.read_text() == "BAZ=qux\n"
        assert "Not found" in _invoke("get", "FOO").output
        assert ssh.disconnected

    def test_set_updates_in_place_and_leaves_no_temp_files(self, session):
        ssh, env_path = session
        env_path.write_text("FOO=old\n")
        assert _invoke("set", "FOO=new").exit_code == 0
        assert env_path.read_text() == "FOO=new\n"
        assert [p.name for p in env_path.parent.iterdir()] == [".env"]
        assert list(ssh.root_dir.iterdir()) == []

    def test_set_replaces_symlink_without_touching_target(self, session, tmp_path):
        _ssh, env_path = session
        victim = tmp_path / "victim.txt"
        victim.write_text("keep")
        env_path.symlink_to(victim)
        assert _invoke("set", "FOO=bar").exit_code == 0
        assert victim.read_text() == "keep"
        assert not env_path.is_symlink()
        assert env_path.read_text() == "FOO=bar\n"

    def test_absent_file_lists_empty(self, session):
        assert "No environment variables" in _invoke("list").output


@pytest.mark.unit
class TestNonRootPathNeverEmitsRunuser:
    """Fails if a command issued from a ``dango`` session would call runuser."""

    def test_all_remote_env_commands_issue_no_runuser(self, tmp_path):
        root_dir = tmp_path / "root"
        root_dir.mkdir()
        bin_dir = tmp_path / "bin"
        _install_shims(bin_dir, root_session=False)
        ssh = ShellSSH("dango", root_dir, bin_dir)
        env_path = tmp_path / ".env"
        with (
            patch.object(remote_env, "_REMOTE_ENV_PATH", str(env_path)),
            patch(
                "dango.cli.commands.remote._ssh_connect_or_fail",
                return_value=(SimpleNamespace(), ssh, tmp_path),
            ),
        ):
            for args in (["set", "A=1"], ["get", "A"], ["list"], ["delete", "A"]):
                assert _invoke(*args).exit_code == 0
        assert ssh.issued
        assert not [c for c in ssh.issued if "runuser" in c], ssh.issued
