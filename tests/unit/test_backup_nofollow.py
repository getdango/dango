"""tests/unit/test_backup_nofollow.py

Root-side reads of project files during backup never follow links planted in the
service user's tree; remaining root Python steps run isolated.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from dango.platform.cloud.ssh import CommandResult
from tests.factories.cloud_factories import host_python_server

_YAML = "url: http://localhost:3000\nadmin:\n  email: a@example.com\n  password: secret-pw\n"
_OTHER = "token: root-readable-value\n"


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    return root


def _run_remote_script(source: Path, destination: Path) -> subprocess.CompletedProcess[str]:
    """Run the SSH-side sanitizer script locally, as root would, against *source*."""
    from dango.platform.cloud.backup import _sanitize_remote_metabase_yaml

    ssh = MagicMock()
    ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
    _sanitize_remote_metabase_yaml(ssh, str(source), str(destination))
    command = ssh.exec_command.call_args[0][0]
    parts = shlex.split(command.split("&&", 1)[1])
    assert parts[1:3] == ["-I", "-c"], parts
    return subprocess.run(
        [sys.executable, "-I", "-c", parts[3], parts[4], parts[5]],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


@pytest.mark.unit
class TestLocalSanitizerNoFollow:
    def test_regular_file_is_sanitized(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import _sanitized_metabase_yaml

        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.write_text(_YAML)
        sanitized = yaml.safe_load(_sanitized_metabase_yaml(source))
        assert sanitized["admin"] == {"email": "a@example.com"}

    def test_symlinked_file_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import _sanitized_metabase_yaml

        target = tmp_path / "elsewhere.yml"
        target.write_text(_OTHER)
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.symlink_to(target)
        with pytest.raises(ValueError):
            _sanitized_metabase_yaml(source)

    def test_symlinked_dango_dir_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import _sanitized_metabase_yaml

        other = tmp_path / "otherdir"
        other.mkdir()
        (other / "metabase.yml").write_text(_OTHER)
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dango").symlink_to(other, target_is_directory=True)
        with pytest.raises(ValueError):
            _sanitized_metabase_yaml(root / ".dango" / "metabase.yml")

    def test_oversized_file_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import _sanitized_metabase_yaml

        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.write_text("a: " + "x" * (2 * 1024 * 1024))
        with pytest.raises(ValueError):
            _sanitized_metabase_yaml(source)

    def test_write_omits_destination_for_symlink(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import _write_sanitized_metabase_yaml

        target = tmp_path / "elsewhere.yml"
        target.write_text(_OTHER)
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.symlink_to(target)
        dest = tmp_path / "staging" / ".dango" / "metabase.yml"
        with pytest.raises(ValueError):
            _write_sanitized_metabase_yaml(source, dest)
        assert not dest.exists()


@pytest.mark.unit
class TestRemoteSanitizerScriptNoFollow:
    def test_regular_file_is_sanitized(self, tmp_path: Path) -> None:
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.write_text(_YAML)
        dest = tmp_path / "out" / "metabase.yml"
        proc = _run_remote_script(source, dest)
        assert proc.returncode == 0, proc.stderr
        assert yaml.safe_load(dest.read_text())["admin"] == {"email": "a@example.com"}

    def test_symlinked_file_is_skipped(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere.yml"
        target.write_text(_OTHER)
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.symlink_to(target)
        dest = tmp_path / "out" / "metabase.yml"
        proc = _run_remote_script(source, dest)
        assert proc.returncode != 0
        assert not dest.exists()

    def test_symlinked_dango_dir_is_skipped(self, tmp_path: Path) -> None:
        other = tmp_path / "otherdir"
        other.mkdir()
        (other / "metabase.yml").write_text(_OTHER)
        root = tmp_path / "project"
        root.mkdir()
        (root / ".dango").symlink_to(other, target_is_directory=True)
        dest = tmp_path / "out" / "metabase.yml"
        proc = _run_remote_script(root / ".dango" / "metabase.yml", dest)
        assert proc.returncode != 0
        assert not dest.exists()

    def test_symlink_warning_is_generic(self) -> None:
        from dango.platform.cloud.backup import _sanitize_remote_metabase_yaml

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="boom", exit_code=1)
        with patch("dango.platform.cloud.backup._logger.warning") as warning:
            result = _sanitize_remote_metabase_yaml(ssh, "/s/.dango/metabase.yml", "/d/m.yml")
        assert result is not None
        warning.assert_called_once_with("metabase_backup_metadata_omitted")


@pytest.mark.unit
class TestScheduledBackupNoFollow:
    def test_scheduled_writer_is_the_nofollow_one(self, tmp_path: Path) -> None:
        """The systemd path imports the same helper, so it refuses links too."""
        from dango.platform.cloud import backup, scheduled_backup

        assert scheduled_backup is not None
        target = tmp_path / "elsewhere.yml"
        target.write_text(_OTHER)
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.symlink_to(target)
        with pytest.raises(ValueError):
            backup._write_sanitized_metabase_yaml(source, tmp_path / "s" / "m.yml")


@pytest.mark.unit
class TestSetupAndMigrateRootPython:
    @pytest.fixture(autouse=True)
    def _host_python(self) -> Iterator[None]:
        with host_python_server():
            yield

    def test_spacy_download_runs_isolated_from_root(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        commands: list[str] = []

        def _exec(command: str, *args: object, **kwargs: object) -> CommandResult:
            commands.append(command)
            code = 1 if command.startswith("test -x") else 0
            return CommandResult(stdout="", stderr="", exit_code=code)

        ssh = MagicMock()
        ssh.exec_command.side_effect = _exec
        _setup_venv(ssh, SetupResult(), None)
        spacy = [c for c in commands if "spacy download" in c]
        assert spacy, commands
        for command in spacy:
            assert re.match(r"cd / && /srv/dango/venv/bin/python -I -m spacy", command), command

    def test_spacy_failure_message_uses_isolated_command(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_venv

        def _exec(command: str, *args: object, **kwargs: object) -> CommandResult:
            code = 1 if command.startswith("test -x") or "spacy" in command else 0
            return CommandResult(stdout="", stderr="", exit_code=code)

        ssh = MagicMock()
        ssh.exec_command.side_effect = _exec
        result = SetupResult()
        _setup_venv(ssh, result, None)
        assert result.warnings

    def test_migrate_uses_shared_root_python(self) -> None:
        from dango.platform.cloud import migrate
        from dango.platform.cloud.runtime_ownership import ROOT_PYTHON

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        migrate._run_transfer_script(ssh, "print(1)")
        run = [c[0][0] for c in ssh.exec_command.call_args_list if "transfer.py" in c[0][0]]
        assert any(c.startswith(f"{ROOT_PYTHON} ") for c in run), run


_FIFO_RUNNER = """
import sys
from pathlib import Path
from dango.platform.cloud.backup import _sanitized_metabase_yaml
try:
    _sanitized_metabase_yaml(Path(sys.argv[1]))
except ValueError:
    raise SystemExit(0)
raise SystemExit(3)
"""


@pytest.mark.unit
class TestSpecialAndLinkedFiles:
    def test_fifo_is_refused_without_blocking(self, tmp_path: Path) -> None:
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        os.mkfifo(source)
        proc = subprocess.run(
            [sys.executable, "-c", _FIFO_RUNNER, str(source)],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr

    def test_remote_script_refuses_fifo_without_blocking(self, tmp_path: Path) -> None:
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        os.mkfifo(source)
        dest = tmp_path / "out" / "metabase.yml"
        proc = _run_remote_script(source, dest)
        assert proc.returncode != 0
        assert not dest.exists()

    def test_hard_linked_file_is_refused(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import _sanitized_metabase_yaml

        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.write_text(_YAML)
        os.link(source, tmp_path / "second-name.yml")
        with pytest.raises(ValueError):
            _sanitized_metabase_yaml(source)

    def test_remote_script_refuses_hard_link(self, tmp_path: Path) -> None:
        source = _project(tmp_path) / ".dango" / "metabase.yml"
        source.write_text(_YAML)
        os.link(source, tmp_path / "second-name.yml")
        dest = tmp_path / "out" / "metabase.yml"
        proc = _run_remote_script(source, dest)
        assert proc.returncode != 0
        assert not dest.exists()


_SIGTERM_RUNNER = """
import os, signal, sys
from pathlib import Path
from unittest.mock import patch
from dango.platform.cloud import scheduled_backup as sb

def terminate():
    os.kill(os.getpid(), signal.SIGTERM)
    return []

with (
    patch.object(sb, "BACKUP_DIR", Path(sys.argv[1])),
    patch.object(sb, "_load_backup_config", return_value={}),
    patch.object(sb, "_acquire_backup_lock", return_value=None),
    patch.object(sb, "_stop_services"),
    patch.object(sb, "_start_services", side_effect=lambda: print("RESTARTED", flush=True)),
    patch.object(sb, "_checkpoint_databases", side_effect=terminate),
):
    try:
        sb.run_scheduled_backup()
    except SystemExit:
        print("EXITED", flush=True)
        raise
"""


@pytest.mark.unit
class TestScheduledBackupSigterm:
    def test_sigterm_restarts_services(self, tmp_path: Path) -> None:
        proc = subprocess.run(
            [sys.executable, "-c", _SIGTERM_RUNNER, str(tmp_path / "backups")],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert "RESTARTED" in proc.stdout, (proc.returncode, proc.stderr)
        assert "EXITED" in proc.stdout
