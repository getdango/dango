"""tests/unit/test_g4f1_r1_cloud.py

G4F1 review round 1 (M1, M2, L1, L2): root reads of project files run as dango, the
build-pending marker cannot be followed or opened, upgrade reporting is honest, and migrate
tells an unreadable secret file from a missing one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud.ssh import CommandResult
from tests.factories.cloud_factories import host_python_server

_AS_DANGO = "runuser -u dango --"


@pytest.mark.unit
class TestStatusReadsRunAsDango:
    def test_sync_history_read_as_dango(self) -> None:
        from dango.platform.cloud.server_status import _get_sync_history

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=0)
        _get_sync_history(ssh)
        assert ssh.exec_command.call_args[0][0].startswith(_AS_DANGO)

    def test_duckdb_size_read_as_dango(self) -> None:
        from dango.platform.cloud.server_status import _get_duckdb_size

        ssh = MagicMock()
        ssh.exec_command.return_value = CommandResult(stdout="12", stderr="", exit_code=0)
        assert _get_duckdb_size(ssh) == 12
        assert ssh.exec_command.call_args[0][0].startswith(_AS_DANGO)


class RacingServer:
    """Runs the marker commands in a sandbox; ``rm`` re-plants a link to a FIFO right after it."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "srv" / "dango"
        self.root.mkdir(parents=True)
        self.fifo = tmp_path / "fifo"
        os.mkfifo(self.fifo)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "rm"
        shim.write_text(
            '#!/bin/sh\n/bin/rm "$@"\n'
            f'for a in "$@"; do case "$a" in *deploy-build-pending) ln -s {self.fifo} "$a";; esac; done\n'
        )
        shim.chmod(0o755)
        self.path = f"{bin_dir}:/usr/bin:/bin"
        self.timed_out = False

    def exec_command(self, command: str, timeout: int | None = None, **_: object) -> CommandResult:
        try:
            proc = subprocess.run(
                ["sh", "-c", command.replace("/srv/dango", str(self.root))],
                capture_output=True,
                text=True,
                env={"PATH": self.path},
                timeout=4,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.timed_out = True
            return CommandResult(stdout="", stderr="timeout", exit_code=124)
        return CommandResult(stdout=proc.stdout, stderr=proc.stderr, exit_code=proc.returncode)


@pytest.mark.unit
class TestMarkerCannotBeFollowedOrOpened:
    def test_a_link_to_a_fifo_planted_during_the_create_is_never_opened(
        self, tmp_path: Path
    ) -> None:
        from dango.platform.cloud.deploy_pending import mark_build_pending

        server = RacingServer(tmp_path)
        with pytest.raises(CloudProvisioningError):
            mark_build_pending(server)
        assert not server.timed_out, "root opened the planted FIFO and blocked"

    def test_mark_probe_and_clear_round_trip(self, tmp_path: Path) -> None:
        from dango.platform.cloud.deploy_pending import (
            build_pending,
            clear_build_pending,
            mark_build_pending,
        )

        server = RacingServer(tmp_path)
        (tmp_path / "bin" / "rm").unlink()  # no racing shim: plain behaviour
        assert build_pending(server) is False
        mark_build_pending(server)
        assert build_pending(server) is True
        assert clear_build_pending(server) is True
        assert build_pending(server) is False


@pytest.fixture
def _trusted_server_venv() -> Any:
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        host_python_server(),
    ):
        yield


def _ssh(*, migration_exit: int) -> MagicMock:
    def _exec(command: str, timeout: int | None = None, **_: object) -> CommandResult:
        if "apply_all_pending" in command:
            return CommandResult(stdout="", stderr="migration error", exit_code=migration_exit)
        if "import dango" in command and "__version__" in command:
            return CommandResult(stdout="1.0.0", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    return ssh


@pytest.mark.unit
@pytest.mark.usefixtures("_trusted_server_venv")
class TestUpgradeReportsAFailedMigration:
    def test_normal_path_is_not_called_healthy(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        with (
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version", return_value="1.1.0"
            ),
            patch("dango.platform.cloud.backup.verify_health", return_value=True),
            patch(
                "dango.platform.cloud.backup.create_backup",
                return_value=MagicMock(archive_path="/srv/dango/backups/x.tar.gz"),
            ),
        ):
            result = upgrade_dango(_ssh(migration_exit=1))

        assert result.migrations_run is False
        assert result.health_check_passed is False


def _printed(result: Any) -> str:
    from dango.cli import console
    from dango.cli.commands import remote_report

    with console.capture() as captured:
        remote_report.upgrade_result(result, "Upgrade complete")
    return str(captured.get())


def _result(**kwargs: Any) -> Any:
    from dango.platform.cloud.upgrade import UpgradeResult

    fields: dict[str, Any] = {
        "old_version": "1.1.0",
        "new_version": "1.1.0",
        "backup_path": None,
        "migrations_run": True,
        "docker_rebuilt": False,
        "health_check_passed": True,
        "duration_seconds": 1.0,
    }
    fields.update(kwargs)
    return UpgradeResult(**fields)


@pytest.mark.unit
class TestUpgradeReportWording:
    def test_failed_migration_says_so_and_does_not_suggest_a_rollback_at_target(self) -> None:
        text = _printed(_result(migrations_run=False, health_check_passed=False))
        assert "Migrations did not complete" in text
        assert "rollback" not in text
        assert "Health check failed" not in text

    def test_failed_migration_after_an_install_says_so(self) -> None:
        text = _printed(
            _result(
                old_version="1.0.0",
                new_version="1.1.0",
                migrations_run=False,
                health_check_passed=False,
            )
        )
        assert "Migrations did not complete" in text

    def test_failed_health_at_target_does_not_suggest_a_rollback(self) -> None:
        text = _printed(_result(health_check_passed=False))
        assert "Health check failed" in text
        assert "rollback" not in text

    def test_failed_health_after_an_install_still_suggests_a_rollback(self) -> None:
        text = _printed(_result(old_version="1.0.0", health_check_passed=False))
        assert "rollback" in text


@pytest.mark.unit
class TestMigrateTellsUnreadableFromMissing:
    def test_unreadable_secret_file_is_reported_as_unreadable(self) -> None:
        from dango.platform.cloud.migrate import _copy_secrets_between_servers

        old_ssh = MagicMock()
        old_ssh.exec_command.return_value = CommandResult(
            stdout="", stderr="Permission denied", exit_code=1
        )
        new_ssh = MagicMock()
        warnings = _copy_secrets_between_servers(old_ssh, new_ssh)
        assert len(warnings) == 2
        assert all("not found" not in w and "Could not read" in w for w in warnings)
        assert new_ssh.write_remote_file.call_count == 0

    def test_missing_secret_file_is_reported_as_not_found(self) -> None:
        from dango.platform.cloud.migrate import _copy_secrets_between_servers

        old_ssh = MagicMock()
        old_ssh.exec_command.return_value = CommandResult(stdout="", stderr="", exit_code=3)
        warnings = _copy_secrets_between_servers(old_ssh, MagicMock())
        assert len(warnings) == 2 and all("not found" in w for w in warnings)
