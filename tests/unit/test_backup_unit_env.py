"""tests/unit/test_backup_unit_env.py

The root backup unit and root Spaces transfers never load the project .env as environment.
"""

from __future__ import annotations

import os
import re
import runpy
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from dango.platform.cloud.ssh import CommandResult

_PROJECT = "/srv/dango/project"
_VENV_PYTHON_RE = re.compile(r"/srv/dango/venv/bin/python3?(?=\s|$)")
_SOURCE_ENV_RE = re.compile(r"(^|[\s;&|(])(source|\.)\s+\S*\.env\b")


def _unit_directives(unit: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for line in unit.splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            directives.setdefault(key.strip(), []).append(value.strip())
    return directives


def _recording_ssh(
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> tuple[MagicMock, list[str]]:
    """Return an SSH mock plus the list every executed command is appended to."""
    results = exec_results or {}
    commands: list[str] = []

    def _exec(command: str, *args: object, **kwargs: object) -> CommandResult:
        commands.append(command)
        for substr, (stdout, stderr, code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    ssh.write_remote_file = MagicMock()
    return ssh, commands


def _assert_isolated_root_python(command: str) -> None:
    """Root Python must run isolated (-I) and never from the dango-writable project dir."""
    match = _VENV_PYTHON_RE.search(command)
    assert match is not None, command
    after = command[match.end() :].split()
    assert after and after[0] == "-I", f"root interpreter not started with -I: {command}"
    assert f"cd {_PROJECT}" not in command, f"root Python runs from the project dir: {command}"


# ---------------------------------------------------------------------------
# Unit templates
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBackupUnit:
    def test_backup_unit_loads_no_environment_file(self) -> None:
        from dango.platform.cloud._server_templates import SYSTEMD_BACKUP_SERVICE

        directives = _unit_directives(SYSTEMD_BACKUP_SERVICE)
        assert "EnvironmentFile" not in directives, directives.get("EnvironmentFile")

    def test_backup_unit_runs_isolated_interpreter_outside_project(self) -> None:
        from dango.platform.cloud._server_templates import SYSTEMD_BACKUP_SERVICE

        directives = _unit_directives(SYSTEMD_BACKUP_SERVICE)
        assert directives["User"] == ["root"]
        (exec_start,) = directives["ExecStart"]
        assert exec_start.split()[:2] == ["/srv/dango/venv/bin/python", "-I"], exec_start
        (workdir,) = directives["WorkingDirectory"]
        assert not workdir.startswith(_PROJECT), workdir
        # No UMask: it would make archive members (and restored files) unreadable to Metabase.
        assert "UMask" not in directives, directives["UMask"]

    def test_logrotate_rotates_project_logs_as_dango(self) -> None:
        """logrotate runs as root; in a dango-owned directory it must drop to dango."""
        from dango.platform.cloud._server_templates import LOGROTATE_CONF

        assert re.search(r"^\s*su dango dango\s*$", LOGROTATE_CONF, re.M), LOGROTATE_CONF


# ---------------------------------------------------------------------------
# Scheduled backup reads only the Spaces keys from .env, as data
# ---------------------------------------------------------------------------

_ENV = (
    "SPACES_ACCESS_KEY=AKEXAMPLE\n"
    'SPACES_SECRET_KEY="secret-example"\n'
    "LD_PRELOAD=/tmp/x.so\n"
    "PYTHONPATH=/tmp\n"
)


@pytest.fixture
def server_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import dango.platform.cloud.scheduled_backup as sb

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(sb, "PROJECT_DIR", project)
    for name in ("SPACES_ACCESS_KEY", "SPACES_SECRET_KEY", "LD_PRELOAD", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    return project


@pytest.mark.unit
class TestScheduledBackupSpacesKeys:
    def test_spaces_client_reads_keys_from_env_file(self, server_project: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _make_spaces_client

        (server_project / ".env").write_text(_ENV)
        client = _make_spaces_client({"bucket": "b", "region": "sgp1"})
        assert client._access_key == "AKEXAMPLE"
        assert client._secret_key == "secret-example"

    def test_env_file_is_not_exported_to_the_root_process(self, server_project: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _make_spaces_client

        (server_project / ".env").write_text(_ENV)
        _make_spaces_client({"bucket": "b", "region": "sgp1"})
        for name in ("LD_PRELOAD", "PYTHONPATH", "SPACES_ACCESS_KEY", "SPACES_SECRET_KEY"):
            assert name not in os.environ, name

    def test_read_env_values_returns_only_requested_names(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import read_env_values

        env = tmp_path / ".env"
        env.write_text(_ENV)
        assert read_env_values(env, ("SPACES_ACCESS_KEY", "MISSING")) == {
            "SPACES_ACCESS_KEY": "AKEXAMPLE"
        }

    def test_symlinked_env_file_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import read_env_values

        target = tmp_path / "elsewhere.env"
        target.write_text(_ENV)
        link = tmp_path / ".env"
        link.symlink_to(target)
        assert read_env_values(link, ("SPACES_ACCESS_KEY",)) == {}

    def test_fifo_env_file_is_refused_without_blocking(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import read_env_values

        fifo = tmp_path / ".env"
        os.mkfifo(fifo)
        assert read_env_values(fifo, ("SPACES_ACCESS_KEY",)) == {}

    def test_oversized_env_file_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import read_env_values

        env = tmp_path / ".env"
        env.write_text("SPACES_ACCESS_KEY=AK\n" + "#" * (2 * 1024 * 1024))
        assert read_env_values(env, ("SPACES_ACCESS_KEY",)) == {}

    def test_missing_env_file_gives_no_values(self, tmp_path: Path) -> None:
        from dango.platform.cloud.scheduled_backup import read_env_values

        assert read_env_values(tmp_path / ".env", ("SPACES_ACCESS_KEY",)) == {}


# ---------------------------------------------------------------------------
# Migration Spaces transfer: no shell `source`, isolated interpreter, private script dir
# ---------------------------------------------------------------------------


def _spaces_config() -> MagicMock:
    return MagicMock(
        region="sgp1",
        bucket="b",
        access_key_env="SPACES_ACCESS_KEY",
        secret_key_env="SPACES_SECRET_KEY",
    )


def _transfer_commands(direction: str) -> tuple[MagicMock, list[str]]:
    from dango.platform.cloud.migrate import (
        _download_backup_from_spaces,
        _upload_backup_to_spaces,
    )

    ssh, commands = _recording_ssh()
    if direction == "upload":
        _upload_backup_to_spaces(ssh, "/srv/dango/backups/deploy/b.tar.gz", _spaces_config())
    else:
        _download_backup_from_spaces(ssh, "migration/b.tar.gz", _spaces_config())
    return ssh, commands


@pytest.mark.unit
@pytest.mark.parametrize("direction", ["upload", "download"])
class TestMigrateSpacesTransfer:
    def test_env_is_never_sourced(self, direction: str) -> None:
        _ssh, commands = _transfer_commands(direction)
        assert not any(_SOURCE_ENV_RE.search(c) for c in commands), commands

    def test_script_runs_isolated(self, direction: str) -> None:
        _ssh, commands = _transfer_commands(direction)
        (command,) = [c for c in commands if _VENV_PYTHON_RE.search(c)]
        _assert_isolated_root_python(command)

    def test_script_lives_in_a_fresh_private_directory(self, direction: str) -> None:
        ssh, commands = _transfer_commands(direction)
        (script_path,) = [c.args[0] for c in ssh.write_remote_file.call_args_list]
        script_dir = script_path.rsplit("/", 1)[0]
        assert script_dir != "/tmp", script_path
        assert re.fullmatch(r"/tmp/dango-migrate-[0-9a-f]{32}", script_dir), script_dir
        assert f"mkdir -m 0700 {script_dir}" in commands, commands
        assert any(c.startswith("rm -rf") and script_dir in c for c in commands), commands


def _run_generated_script(
    script: str, env_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict:
    """Execute a generated transfer script against a stub boto3 and a temp .env."""
    calls: dict = {}

    class _Client:
        def upload_file(self, *args: object) -> None:
            calls["upload"] = args

        def download_file(self, *args: object) -> None:
            calls["download"] = args

    def _client(*args: object, **kwargs: object) -> _Client:
        calls["client"] = kwargs
        return _Client()

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=_client))
    script = script.replace(repr(f"{_PROJECT}/.env"), repr(str(env_file)))
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)
    script_file = tmp_path / "transfer.py"
    script_file.write_text(script)
    runpy.run_path(str(script_file), run_name="__main__")
    return calls


@pytest.mark.unit
class TestGeneratedTransferScripts:
    def _upload_script(self) -> str:
        from dango.platform.cloud.migrate import _build_spaces_upload_script

        return _build_spaces_upload_script(
            archive_path="/srv/dango/backups/deploy/b.tar.gz",
            region="sgp1",
            endpoint="https://sgp1.digitaloceanspaces.com",
            access_key_env="SPACES_ACCESS_KEY",
            secret_key_env="SPACES_SECRET_KEY",
            bucket="b",
            spaces_key="migration/b.tar.gz",
        )

    def test_script_does_not_import_dango(self) -> None:
        """The old server may still run an older Dango: the script carries its own reader."""
        import ast

        tree = ast.parse(self._upload_script())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert "dango" not in imported, imported

    def test_script_reads_keys_from_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name in ("SPACES_ACCESS_KEY", "SPACES_SECRET_KEY"):
            monkeypatch.delenv(name, raising=False)
        env = tmp_path / ".env"
        env.write_text(_ENV)
        calls = _run_generated_script(self._upload_script(), env, tmp_path, monkeypatch)
        assert calls["client"]["aws_access_key_id"] == "AKEXAMPLE"
        assert calls["client"]["aws_secret_access_key"] == "secret-example"
        assert "upload" in calls

    def test_script_fails_clearly_without_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        env = tmp_path / ".env"
        env.write_text("OTHER=1\n")
        with pytest.raises(SystemExit, match="SPACES_ACCESS_KEY"):
            _run_generated_script(self._upload_script(), env, tmp_path, monkeypatch)
