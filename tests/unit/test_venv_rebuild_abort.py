"""tests/unit/test_venv_rebuild_abort.py

A venv that cannot be rebuilt as root-owned aborts the upgrade; at target, a rebuild asks first.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError, CloudProvisioningError
from tests.unit.test_runtime_ownership import _recording_ssh, _run_upgrade

_VENV_EXEC = re.compile(r"/srv/dango/venv/bin/(python3?|pip|dango)\b")
_FAIL_VENV = {"python3 -m venv /srv/dango/venv": ("", "no space left", 1)}


def _after_restore(commands: list[str]) -> list[str]:
    restored = max(
        i
        for i, c in enumerate(commands)
        if re.search(r"mv /srv/dango/venv\.untrusted-\S+ /srv/dango/venv", c)
    )
    return commands[restored + 1 :]


@pytest.mark.unit
class TestFailedRebuildAborts:
    def test_failed_rebuild_aborts_with_a_clear_error(self) -> None:
        with pytest.raises(CloudError, match="upgrade aborted") as excinfo:
            _run_upgrade("1.0.14", "1.0.15", venv_untrusted=True, extra=_FAIL_VENV)
        assert "server unchanged" in str(excinfo.value)

    def test_failed_rebuild_restores_and_restarts_without_running_the_old_venv(self) -> None:
        from dango.platform.cloud.upgrade import upgrade_dango

        ssh, commands = _recording_ssh(
            {
                **_FAIL_VENV,
                "-print -quit": ("/srv/dango\n", "", 0),
                "getdango-*.dist-info": ("/v/getdango-1.0.14.dist-info\n", "", 0),
                "curl -sf": ("ok", "", 0),
            }
        )
        with (
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version",
                return_value="1.0.15",
            ),
            patch("dango.platform.cloud.backup.create_backup"),
            pytest.raises(CloudError, match="upgrade aborted"),
        ):
            upgrade_dango(ssh)
        after = _after_restore(commands)
        assert [c for c in after if _VENV_EXEC.search(c)] == [], after
        assert any("systemctl start dango-web" in c for c in after), after
        assert not any(re.search(r"pip install .*getdango==1\.0\.15", c) for c in commands)

    def test_setup_rerun_fails_closed_when_the_rebuild_fails(self) -> None:
        from dango.platform.cloud.server_setup import SetupResult, _setup_runtime_ownership

        ssh, _commands = _recording_ssh(
            {
                **_FAIL_VENV,
                "-print -quit": ("/srv/dango\n", "", 0),
                "getdango-*.dist-info": ("/v/getdango-1.0.13.dist-info\n", "", 0),
            }
        )
        with pytest.raises(CloudProvisioningError, match="venv"):
            _setup_runtime_ownership(ssh, SetupResult(), None)


# ---------------------------------------------------------------------------
# CLI at target: a rebuild (restarts services) asks first; idempotent steps do not
# ---------------------------------------------------------------------------


def _cli(
    tmp_path: Path, args: list[str], *, untrusted: bool, confirm: Any
) -> tuple[str, MagicMock]:
    from click.testing import CliRunner

    from dango.cli.commands.remote import remote

    ssh, _commands = _recording_ssh(
        {
            "import dango": ("1.0.15", "", 0),
            "getdango-*.dist-info": ("/v/getdango-1.0.15.dist-info\n", "", 0),
        }
    )
    upgrade = MagicMock(
        return_value=MagicMock(
            health_check_passed=True,
            warnings=[],
            backup_path=None,
            migrations_run=False,
            docker_rebuilt=False,
            caddyfile_ws_migration=None,
            runtime_ownership="Runtime ownership applied",
            venv_rebuild=None,
        )
    )
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch(
            "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
            return_value=(MagicMock(), ssh),
        ),
        patch(
            "dango.platform.cloud.server_status.check_latest_pypi_version", return_value="1.0.15"
        ),
        patch("dango.platform.cloud.runtime_ownership.venv_is_untrusted", return_value=untrusted),
        patch("dango.platform.cloud.upgrade.upgrade_dango", upgrade),
        patch("dango.cli.commands.remote_ops.safe_confirm", confirm),
    ):
        result = CliRunner().invoke(remote, args, obj={"project_root": tmp_path})
    return result.output, upgrade


@pytest.mark.unit
class TestAtTargetConfirmation:
    def test_rebuild_needed_and_declined_does_nothing(self, tmp_path: Path) -> None:
        confirm = MagicMock(return_value=False)
        output, upgrade = _cli(tmp_path, ["upgrade"], untrusted=True, confirm=confirm)
        assert confirm.called
        assert "rebuild" in output.lower()
        assert not upgrade.called

    def test_rebuild_needed_with_yes_does_not_prompt(self, tmp_path: Path) -> None:
        confirm = MagicMock(side_effect=AssertionError("must not prompt"))
        _output, upgrade = _cli(tmp_path, ["upgrade", "--yes"], untrusted=True, confirm=confirm)
        assert upgrade.called

    def test_idempotent_steps_only_do_not_prompt(self, tmp_path: Path) -> None:
        confirm = MagicMock(side_effect=AssertionError("must not prompt"))
        _output, upgrade = _cli(tmp_path, ["upgrade"], untrusted=False, confirm=confirm)
        assert upgrade.called
