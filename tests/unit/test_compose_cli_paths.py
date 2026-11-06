"""tests/unit/test_compose_cli_paths.py

CLI paths that must honour the compose helper: `dango start` orphan cleanup is skipped
and `dango metabase repair-admin` is accepted when the helper marker is present.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.metabase_cmd import metabase
from tests.unit.test_start_serve_metabase_ready_order import _run_local

_USES_HELPER = "dango.platform.compose_exec.uses_helper"
_PROJECT_NAME = "dango.platform.docker.get_compose_project_name"
_MANAGER = "dango.platform.DockerManager"
_REPAIR = "dango.platform.common.metabase_admin_repair.repair_admin_credential"


@pytest.mark.unit
class TestStartOrphanCleanup:
    def test_skipped_when_helper_in_use(self, tmp_path: Path) -> None:
        with (
            patch(_USES_HELPER, return_value=True),
            patch(_PROJECT_NAME, return_value="dango-abc") as name,
            patch(_MANAGER) as manager,
        ):
            result, _events = _run_local(tmp_path)

        assert result.exit_code == 0, result.output
        name.assert_not_called()
        manager.assert_not_called()
        assert "orphaned" not in result.output

    def test_legacy_still_checks_for_orphans(self, tmp_path: Path) -> None:
        with (
            patch(_USES_HELPER, return_value=False),
            patch(_PROJECT_NAME, return_value="dango-abc") as name,
        ):
            result, _events = _run_local(tmp_path)

        assert result.exit_code == 0, result.output
        name.assert_called_once()


@pytest.mark.unit
class TestRepairAdminHelper:
    def _invoke(self, root: Path, helper: bool) -> tuple[int, MagicMock]:
        with (
            patch("dango.cli.utils.require_project_context", return_value=root),
            patch(_USES_HELPER, return_value=helper),
            patch(_REPAIR, return_value={"status": "repaired"}) as repair,
        ):
            out = CliRunner().invoke(metabase, ["repair-admin"], obj={})
        return out.exit_code, repair

    def test_accepts_helper_without_project_compose_file(self, tmp_path: Path) -> None:
        code, repair = self._invoke(tmp_path, helper=True)
        assert code == 0
        repair.assert_called_once()

    def test_still_refuses_without_either(self, tmp_path: Path) -> None:
        code, repair = self._invoke(tmp_path, helper=False)
        assert code != 0
        repair.assert_not_called()
