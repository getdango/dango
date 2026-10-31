"""tests/unit/test_backup_silent_handlers.py

Best-effort fallbacks in the SSH backup module log a warning instead of passing silently.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud.ssh import CommandResult

_B = "dango.platform.cloud.backup"


def _ssh(stdout: str = "", exit_code: int = 0, raises: Exception | None = None) -> MagicMock:
    ssh = MagicMock()
    if raises is not None:
        ssh.exec_command.side_effect = raises
    else:
        ssh.exec_command.return_value = CommandResult(stdout=stdout, stderr="", exit_code=exit_code)
    return ssh


def _events(logger: MagicMock) -> list[str]:
    return [c.args[0] for c in logger.warning.call_args_list]


@pytest.mark.unit
def test_unreadable_remote_project_id_logs_legacy_hash_fallback() -> None:
    from dango.platform.cloud.backup import get_remote_compose_project_name

    with patch(f"{_B}._logger") as logger:
        name = get_remote_compose_project_name(_ssh(raises=OSError("connection reset")))
    assert name.startswith("dango-")
    assert "remote_project_id_unreadable_using_legacy_hash" in _events(logger)


@pytest.mark.unit
def test_unparseable_df_logs_skipped_disk_check() -> None:
    from dango.platform.cloud.backup import _check_disk_space

    with patch(f"{_B}._logger") as logger:
        _check_disk_space(_ssh(stdout="/dev/vda1 25000 5000 lots 20% /srv"))
    assert "disk_space_check_skipped" in _events(logger)


@pytest.mark.unit
def test_unparseable_usage_logs_skipped_usage_warning() -> None:
    from dango.platform.cloud.backup import _check_disk_space

    with patch(f"{_B}._logger") as logger:
        _check_disk_space(_ssh(stdout="/dev/vda1 25000 5000 20000 n/a /srv"))
    assert "disk_usage_warning_skipped" in _events(logger)


@pytest.mark.unit
@pytest.mark.parametrize(
    "ssh", [_ssh(exit_code=1), _ssh(raises=OSError("gone"))], ids=["exit", "raises"]
)
def test_failed_cleanup_logs_leftover_paths_and_never_raises(ssh: MagicMock) -> None:
    from dango.platform.cloud.backup import _remove_leftovers

    with patch(f"{_B}._logger") as logger:
        _remove_leftovers(ssh, ["/tmp/dango-restore-staging-x", "/tmp/tmp.private"])
    (call,) = [
        c for c in logger.warning.call_args_list if c.args[0] == "temporary_directory_not_removed"
    ]
    assert call.kwargs["paths"] == ["/tmp/dango-restore-staging-x", "/tmp/tmp.private"]
