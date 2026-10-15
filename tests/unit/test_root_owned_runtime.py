"""tests/unit/test_root_owned_runtime.py

Cloud servers: everything root executes, imports or loads as environment stays root-owned.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.platform.cloud.ssh import CommandResult

_VENV_PYTHON_RE = re.compile(r"/srv/dango/venv/bin/python3?(?=\s|$)")
_PROJECT = "/srv/dango/project"


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


def _root_python_invocations(commands: list[str]) -> list[str]:
    """Commands that start the venv interpreter as root (not via ``sudo -u dango``)."""
    return [
        c for c in commands if _VENV_PYTHON_RE.search(c) and not re.search(r"sudo\s+-u\s+dango", c)
    ]


def _assert_isolated_root_python(command: str) -> None:
    """Root Python must run isolated (-I) and never from the dango-writable project dir."""
    match = _VENV_PYTHON_RE.search(command)
    assert match is not None, command
    after = command[match.end() :].split()
    assert after and after[0] == "-I", f"root interpreter not started with -I: {command}"
    assert f"cd {_PROJECT}" not in command, f"root Python runs from the project dir: {command}"


# ---------------------------------------------------------------------------
# Scheduled backup unit (runs as root every night)
# ---------------------------------------------------------------------------


def _unit_directives(unit: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for line in unit.splitlines():
        if "=" in line and not line.startswith(("#", "[")):
            key, _, value = line.partition("=")
            directives.setdefault(key.strip(), []).append(value.strip())
    return directives


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

    def test_logrotate_rotates_project_logs_as_dango(self) -> None:
        """logrotate runs as root; in a dango-owned directory it must drop to dango."""
        from dango.platform.cloud._server_templates import LOGROTATE_CONF

        assert re.search(r"^\s*su dango dango\s*$", LOGROTATE_CONF, re.M), LOGROTATE_CONF


# ---------------------------------------------------------------------------
# Scheduled backup reads only the Spaces keys from .env, as data
# ---------------------------------------------------------------------------


@pytest.fixture
def _server_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import dango.platform.cloud.scheduled_backup as sb

    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(sb, "PROJECT_DIR", project)
    for name in ("SPACES_ACCESS_KEY", "SPACES_SECRET_KEY", "LD_PRELOAD", "PYTHONPATH"):
        monkeypatch.delenv(name, raising=False)
    return project


@pytest.mark.unit
class TestScheduledBackupSpacesKeys:
    _ENV = (
        "SPACES_ACCESS_KEY=AKEXAMPLE\n"
        'SPACES_SECRET_KEY="secret-example"\n'
        "LD_PRELOAD=/tmp/x.so\n"
        "PYTHONPATH=/tmp\n"
    )

    def test_spaces_client_reads_keys_from_env_file(self, _server_project: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _make_spaces_client

        (_server_project / ".env").write_text(self._ENV)
        client = _make_spaces_client({"bucket": "b", "region": "sgp1"})
        assert client._access_key == "AKEXAMPLE"
        assert client._secret_key == "secret-example"

    def test_env_file_is_not_exported_to_the_root_process(self, _server_project: Path) -> None:
        from dango.platform.cloud.scheduled_backup import _make_spaces_client

        (_server_project / ".env").write_text(self._ENV)
        _make_spaces_client({"bucket": "b", "region": "sgp1"})
        for name in ("LD_PRELOAD", "PYTHONPATH", "SPACES_ACCESS_KEY", "SPACES_SECRET_KEY"):
            assert name not in os.environ, name

    def test_symlinked_env_file_is_not_followed(self, _server_project: Path) -> None:
        from dango.exceptions import CloudAuthError
        from dango.platform.cloud.scheduled_backup import _make_spaces_client

        elsewhere = _server_project.parent / "elsewhere.env"
        elsewhere.write_text(self._ENV)
        (_server_project / ".env").symlink_to(elsewhere)
        with pytest.raises(CloudAuthError):
            _make_spaces_client({"bucket": "b", "region": "sgp1"})


# ---------------------------------------------------------------------------
# Server setup: /srv/dango and the venv end up root-owned
# ---------------------------------------------------------------------------

_FRESH_SERVER = {
    "id -u dango": ("", "no such user", 1),
    "docker --version": ("", "", 1),
    "caddy version": ("", "", 1),
    "test -x /srv/dango/venv/bin/dango": ("", "", 1),
    "cat /": ("", "No such file", 1),
}


@pytest.mark.unit
class TestSetupOwnership:
    def test_setup_never_hands_srv_dango_to_dango(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _recording_ssh(_FRESH_SERVER)
        setup_server(ssh)
        offending = [
            c
            for c in commands
            if re.search(r"chown\s+(-R\s+)?dango:dango\s+/srv/dango(\s|$|/venv|/backups)", c)
        ]
        assert offending == []

    def test_setup_makes_runtime_root_owned_after_venv_install(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _recording_ssh(_FRESH_SERVER)
        setup_server(ssh)
        venv_created = next(
            i for i, c in enumerate(commands) if "python3 -m venv /srv/dango/venv" in c
        )
        later = " ; ".join(commands[venv_created + 1 :])
        assert re.search(r"chown\s+-R\s+root:root\s+/srv/dango/venv\b", later), later
        assert re.search(r"chown\s+root:root\s+/srv/dango(\s|;|&|$)", later), later

    def test_setup_keeps_project_and_secrets_dango_owned(self) -> None:
        from dango.platform.cloud.server_setup import setup_server

        ssh, commands = _recording_ssh(_FRESH_SERVER)
        setup_server(ssh)
        joined = " ; ".join(commands)
        assert re.search(r"chown\s+-R\s+dango:dango\s+/srv/dango/project\b", joined)
        assert re.search(r"chown\s+dango:dango\s+/srv/dango/secrets\b", joined)


# ---------------------------------------------------------------------------
# Upgrade migrates existing servers
# ---------------------------------------------------------------------------


def _run_upgrade(
    installed: str, latest: str, *, backup_unit_present: bool
) -> tuple[MagicMock, list[str]]:
    from dango.platform.cloud.upgrade import upgrade_dango

    results = {
        "import dango": (installed, "", 0),
        "test -f /etc/systemd/system/dango-backup.service": (
            "",
            "",
            0 if backup_unit_present else 1,
        ),
        "curl -sf": ("ok", "", 0),
    }
    ssh, commands = _recording_ssh(results)
    backup_result = MagicMock(archive_path="/srv/dango/backups/deploy/backup-x.tar.gz")
    with (
        patch("dango.platform.cloud.server_status.check_latest_pypi_version", return_value=latest),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup_result),
    ):
        upgrade_dango(ssh)
    return ssh, commands


@pytest.mark.unit
class TestUpgradeMigration:
    def test_upgrade_makes_runtime_root_owned(self) -> None:
        _ssh, commands = _run_upgrade("1.0.14", "1.0.15", backup_unit_present=False)
        joined = " ; ".join(commands)
        assert re.search(r"chown\s+-R\s+root:root\s+/srv/dango/venv\b", joined), joined
        assert re.search(r"chown\s+root:root\s+/srv/dango(\s|;|&|$)", joined), joined

    def test_upgrade_root_python_runs_isolated(self) -> None:
        _ssh, commands = _run_upgrade("1.0.14", "1.0.15", backup_unit_present=False)
        roots = _root_python_invocations(commands)
        assert roots, commands
        for command in roots:
            _assert_isolated_root_python(command)

    def test_upgrade_rewrites_existing_backup_unit(self) -> None:
        from dango.platform.cloud._server_templates import SYSTEMD_BACKUP_SERVICE

        ssh, commands = _run_upgrade("1.0.14", "1.0.15", backup_unit_present=True)
        writes = {c.args[0]: c.args[1] for c in ssh.write_remote_file.call_args_list}
        assert writes.get("/etc/systemd/system/dango-backup.service") == SYSTEMD_BACKUP_SERVICE
        assert any("systemctl daemon-reload" in c for c in commands)


# ---------------------------------------------------------------------------
# Root SSH steps run the interpreter isolated, never from the project dir
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRootPythonIsolated:
    def test_backup_checkpoints(self) -> None:
        from dango.platform.cloud.backup import _checkpoint_auth_db, _checkpoint_duckdb

        ssh, commands = _recording_ssh()
        _checkpoint_duckdb(ssh)
        _checkpoint_auth_db(ssh)
        roots = _root_python_invocations(commands)
        assert len(roots) == 2
        for command in roots:
            _assert_isolated_root_python(command)

    def test_backup_metabase_yaml_sanitizer(self) -> None:
        from dango.platform.cloud.backup import _sanitize_remote_metabase_yaml

        ssh, commands = _recording_ssh()
        _sanitize_remote_metabase_yaml(
            ssh, f"{_PROJECT}/.dango/metabase.yml", "/tmp/x/metabase.yml"
        )
        (command,) = _root_python_invocations(commands)
        _assert_isolated_root_python(command)

    def test_server_status_version_probe(self) -> None:
        from dango.platform.cloud.server_status import _get_dango_version

        ssh, commands = _recording_ssh()
        _get_dango_version(ssh)
        (command,) = _root_python_invocations(commands)
        _assert_isolated_root_python(command)

    def test_remote_repair_commands(self) -> None:
        from dango.cli.commands.remote_repair import (
            _metabase_schema_scan_command,
            _metabase_secret_project_id_command,
        )

        _assert_isolated_root_python(_metabase_schema_scan_command(_PROJECT))
        _assert_isolated_root_python(_metabase_secret_project_id_command(_PROJECT))

    @pytest.mark.parametrize("args", [[], ["restore", "backup-20260101-000000.tar.gz", "--yes"]])
    def test_remote_backup_server_commands(self, args: list[str]) -> None:
        from dango.cli.commands.remote_backup import backup_group

        ssh, commands = _recording_ssh()
        with patch(
            "dango.cli.commands.remote_backup._load_cloud_config_with_ssh_or_fail",
            return_value=(MagicMock(), ssh),
        ):
            CliRunner().invoke(backup_group, args, catch_exceptions=True)
        roots = _root_python_invocations(commands)
        assert roots, commands
        for command in roots:
            _assert_isolated_root_python(command)


# ---------------------------------------------------------------------------
# Migration never sources the project .env in a root shell
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMigrateSpacesTransfer:
    def _spaces_config(self) -> MagicMock:
        return MagicMock(
            region="sgp1",
            bucket="b",
            access_key_env="SPACES_ACCESS_KEY",
            secret_key_env="SPACES_SECRET_KEY",
        )

    def test_upload_does_not_source_env_and_runs_isolated(self) -> None:
        from dango.platform.cloud.migrate import _upload_backup_to_spaces

        ssh, commands = _recording_ssh()
        _upload_backup_to_spaces(ssh, "/srv/dango/backups/deploy/b.tar.gz", self._spaces_config())
        assert not any(re.search(r"(^|[\s;&|])(source|\.)\s+\S*\.env\b", c) for c in commands)
        (command,) = _root_python_invocations(commands)
        _assert_isolated_root_python(command)

    def test_download_does_not_source_env_and_runs_isolated(self) -> None:
        from dango.platform.cloud.migrate import _download_backup_from_spaces

        ssh, commands = _recording_ssh()
        _download_backup_from_spaces(ssh, "migration/b.tar.gz", self._spaces_config())
        assert not any(re.search(r"(^|[\s;&|])(source|\.)\s+\S*\.env\b", c) for c in commands)
        (command,) = _root_python_invocations(commands)
        _assert_isolated_root_python(command)


# ---------------------------------------------------------------------------
# Push: project requirements are installed by root from the pushed content
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPushRequirements:
    @patch("dango.platform.cloud.backup.create_backup")
    @patch("dango.platform.cloud.file_sync.sync_project_files")
    def test_requirements_never_installed_from_the_dango_writable_copy(
        self, mock_sync: MagicMock, mock_backup: MagicMock, tmp_path: Path
    ) -> None:
        from dango.platform.cloud.deployer import push_deploy
        from tests.unit.test_deployer_push import _make_backup_result, _make_sync_result

        mock_sync.return_value = _make_sync_result()
        mock_backup.return_value = _make_backup_result()
        (tmp_path / "requirements.txt").write_text("requests==2.32.3\n")
        ssh, commands = _recording_ssh({"requirements.txt && echo exists": ("exists", "", 0)})
        push_deploy(ssh, tmp_path, "10.0.0.1")

        pip_runs = [c for c in commands if re.search(r"/pip\s+install\b.*-r\b", c)]
        assert pip_runs, commands
        for command in pip_runs:
            assert not re.search(r"sudo\s+-u\s+dango", command), command
            assert f"{_PROJECT}/requirements.txt" not in command, command
            assert f"{_PROJECT}/scripts/requirements.txt" not in command, command
