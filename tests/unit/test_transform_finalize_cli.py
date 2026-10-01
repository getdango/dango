"""tests/unit/test_transform_finalize_cli.py

`dango run` records model status after a failed dbt build (finalize_dbt_build runs
before the failure Abort).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.transform import run


def _invoke(tmp_path: Path, returncode: int) -> tuple[object, MagicMock]:
    (tmp_path / "dbt").mkdir(exist_ok=True)
    fin = MagicMock(
        return_value={
            "model_status": "updated",
            "schema_sync": "updated",
            "metabase": "not_running",
            "metabase_schema_synced": False,
        }
    )
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("subprocess.run", return_value=SimpleNamespace(returncode=returncode)),
        patch("dango.utils.DbtLock"),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
            return_value=False,
        ),
        patch("dango.transformation.build_finalize.finalize_dbt_build", fin),
    ):
        result = CliRunner().invoke(run, [])
    return result, fin


@pytest.mark.unit
def test_run_records_status_on_failure(tmp_path: Path) -> None:
    result, fin = _invoke(tmp_path, 1)
    assert result.exit_code != 0  # type: ignore[attr-defined]
    fin.assert_called_once_with(tmp_path, success=False)


@pytest.mark.unit
def test_run_success_finalizes_and_exits_zero(tmp_path: Path) -> None:
    result, fin = _invoke(tmp_path, 0)
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    fin.assert_called_once_with(tmp_path, success=True)
    assert "Updating schema.yml files" in result.output  # type: ignore[attr-defined]
