"""tests/unit/test_root_python_isolated.py

Root SSH steps run the venv interpreter isolated (-I, from /); database checkpoints run as dango.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.platform.cloud.ssh import CommandResult

_PROJECT = "/srv/dango/project"
_VENV_PYTHON_RE = re.compile(r"/srv/dango/venv/bin/python3?(?=\s|$)")
_AS_DANGO_RE = re.compile(r"(sudo\s+-u\s+dango|runuser\s+-u\s+dango)")


def _recording_ssh(
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> tuple[MagicMock, list[str]]:
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


def _python_invocations(commands: list[str]) -> list[str]:
    return [c for c in commands if _VENV_PYTHON_RE.search(c)]


def _assert_isolated(command: str) -> None:
    """The venv interpreter runs with -I, from /, never from the dango-writable project dir."""
    match = _VENV_PYTHON_RE.search(command)
    assert match is not None, command
    after = command[match.end() :].split()
    assert after and after[0] == "-I", f"interpreter not started with -I: {command}"
    assert re.search(r"(^|&&\s*|;\s*)cd /(\s|$)", command), f"not run from /: {command}"
    assert f"cd {_PROJECT}" not in command, command


@pytest.mark.unit
class TestBackupSteps:
    def test_checkpoints_run_as_dango_isolated(self) -> None:
        from dango.platform.cloud.backup import _checkpoint_auth_db, _checkpoint_duckdb

        ssh, commands = _recording_ssh()
        _checkpoint_duckdb(ssh)
        _checkpoint_auth_db(ssh)
        runs = _python_invocations(commands)
        assert len(runs) == 2, commands
        for command in runs:
            _assert_isolated(command)
            assert re.search(r"runuser -u dango -- /srv/dango/venv/bin/python -I", command), command

    def test_metabase_yaml_sanitizer_is_isolated(self) -> None:
        from dango.platform.cloud.backup import _sanitize_remote_metabase_yaml

        ssh, commands = _recording_ssh()
        _sanitize_remote_metabase_yaml(ssh, f"{_PROJECT}/.dango/metabase.yml", "/tmp/x/m.yml")
        (command,) = _python_invocations(commands)
        _assert_isolated(command)

    def test_backup_version_probe_is_isolated(self) -> None:
        import dango.platform.cloud.backup as backup

        ssh, commands = _recording_ssh({"import dango": ("1.0.14", "", 0)})
        with patch.object(backup, "_run_checked", return_value=""):
            try:
                backup._create_archive(ssh, "20260101-000000", None, "test")
            except Exception:  # the archive steps themselves are not under test here
                pass
        probes = [c for c in commands if "import dango" in c]
        assert probes, commands
        for command in probes:
            _assert_isolated(command)


@pytest.mark.unit
def test_server_status_version_probe_is_isolated() -> None:
    from dango.platform.cloud.server_status import _get_dango_version

    ssh, commands = _recording_ssh()
    _get_dango_version(ssh)
    (command,) = _python_invocations(commands)
    _assert_isolated(command)


@pytest.mark.unit
class TestRemoteRepair:
    def test_secret_project_id_lookup_is_isolated(self) -> None:
        from dango.cli.commands.remote_repair import _metabase_secret_project_id_command

        _assert_isolated(_metabase_secret_project_id_command(_PROJECT))

    def test_schema_scan_is_isolated_and_names_the_project_explicitly(self) -> None:
        from dango.cli.commands.remote_repair import (
            _metabase_schema_scan_command,
            _metabase_schema_scan_script,
        )

        command = _metabase_schema_scan_command(_PROJECT)
        _assert_isolated(command)
        assert "DANGO_CLOUD_MODE=true" in command
        assert _PROJECT in command
        assert 'Path(".")' not in _metabase_schema_scan_script()


@pytest.mark.unit
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["restore", "backup-20260101-000000.tar.gz", "--yes"],
        ["download", "--from-server", "-o", "/tmp/dango-test-download.tar.gz"],
    ],
)
def test_remote_backup_server_commands_are_isolated(args: list[str]) -> None:
    from dango.cli.commands.remote_backup import backup_group

    ssh, commands = _recording_ssh()
    with patch(
        "dango.cli.commands.remote_backup._load_cloud_config_with_ssh_or_fail",
        return_value=(MagicMock(), ssh),
    ):
        CliRunner().invoke(backup_group, args, catch_exceptions=True)
    runs = [c for c in _python_invocations(commands) if not _AS_DANGO_RE.search(c)]
    assert runs, commands
    for command in runs:
        _assert_isolated(command)


@pytest.mark.unit
def test_scheduled_backup_checkpoints_run_as_dango_not_in_the_root_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root opening auth.db in-process leaves root-owned -wal/-shm files dango-web cannot write."""
    import subprocess

    import dango.platform.cloud.scheduled_backup as sb

    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "warehouse.duckdb").write_bytes(b"")
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".dango" / "auth.db").write_bytes(b"")
    monkeypatch.setattr(sb, "PROJECT_DIR", tmp_path)
    runs: list[list[str]] = []

    def _run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runs.append(list(argv))
        assert kwargs.get("cwd") == "/"
        return subprocess.CompletedProcess(argv, 0, "", "")

    with (
        patch.object(sb.subprocess, "run", side_effect=_run),
        patch("duckdb.connect", side_effect=AssertionError("checkpoint ran in the root process")),
        patch("sqlite3.connect", side_effect=AssertionError("checkpoint ran in the root process")),
    ):
        warnings = sb._checkpoint_databases()
    assert warnings == []
    assert len(runs) == 2, runs
    for argv in runs:
        assert argv[:5] == ["runuser", "-u", "dango", "--", "/srv/dango/venv/bin/python"], argv
        assert argv[5] == "-I", argv
