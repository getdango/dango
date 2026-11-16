"""tests/unit/test_g4f1_upgrade_at_target.py

I8-14: repeating ``remote upgrade`` at the target version still applies pending migrations
and reports a real health result, so an earlier run that died after the install can be finished.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud.ssh import CommandResult
from tests.factories.cloud_factories import host_python_server

_PYPI = "dango.platform.cloud.server_status.check_latest_pypi_version"
_HEALTH = "dango.platform.cloud.backup.verify_health"


@pytest.fixture(autouse=True)
def _trusted_server_venv() -> Any:
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        host_python_server(),
    ):
        yield


def _ssh(*, migration_exit: int = 0) -> MagicMock:
    def _exec(command: str, timeout: int | None = None, **_: object) -> CommandResult:
        if "apply_all_pending" in command:
            return CommandResult(stdout="", stderr="migration error", exit_code=migration_exit)
        if "import dango" in command and "__version__" in command:
            return CommandResult(stdout="1.1.0", stderr="", exit_code=0)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec
    return ssh


def _commands(ssh: MagicMock) -> list[str]:
    return [c[0][0] for c in ssh.exec_command.call_args_list]


def _upgrade(ssh: MagicMock) -> Any:
    from dango.platform.cloud.upgrade import upgrade_dango

    with patch(_PYPI, return_value="1.1.0"):
        return upgrade_dango(ssh)


@pytest.mark.unit
class TestAtTargetPath:
    def test_pending_migrations_are_applied(self) -> None:
        ssh = _ssh()
        with patch(_HEALTH, return_value=True):
            result = _upgrade(ssh)

        assert any("apply_all_pending" in c for c in _commands(ssh))
        assert any("sudo -u dango" in c for c in _commands(ssh) if "apply_all_pending" in c)
        assert result.migrations_run is True
        assert result.health_check_passed is True
        assert not any("pip install" in c for c in _commands(ssh))

    def test_a_failed_migration_is_reported_and_not_called_healthy(self) -> None:
        ssh = _ssh(migration_exit=1)
        with patch(_HEALTH, return_value=True):
            result = _upgrade(ssh)

        assert result.migrations_run is False
        assert result.health_check_passed is False
        assert any("Migration command exited with code 1" in w for w in result.warnings)

    def test_a_retry_after_the_failed_migration_applies_it(self) -> None:
        with patch(_HEALTH, return_value=True):
            first = _upgrade(_ssh(migration_exit=1))
            second = _upgrade(_ssh())
        assert first.health_check_passed is False
        assert second.migrations_run is True and second.health_check_passed is True

    def test_health_is_probed_not_assumed(self) -> None:
        ssh = _ssh()
        with patch(_HEALTH, return_value=False) as health:
            result = _upgrade(ssh)

        health.assert_called_once()
        assert result.health_check_passed is False
        assert any("Health check did not pass" in w for w in result.warnings)
