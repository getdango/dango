"""tests/unit/test_run_warned_tests_note.py

`dango run` says plainly, and exits 0, when a build succeeded with warn-severity data tests.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.transform import run
from tests.factories.scaffold_project import make_scaffold


def _plain(output: str) -> str:
    """Output without ANSI codes and with the console's line wrapping collapsed."""
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", output).split())


def _invoke(tmp_path: Path, statuses: list[tuple[str, str]]) -> Result:
    target = tmp_path / "dbt" / "target"
    target.mkdir(parents=True, exist_ok=True)

    def fake_dbt(*_args: object, **_kwargs: object) -> SimpleNamespace:
        results = [
            {"unique_id": f"test.proj.{name}.abc123", "status": status, "message": None}
            for name, status in statuses
        ]
        (target / "run_results.json").write_text(
            json.dumps({"results": results}), encoding="utf-8", newline="\n"
        )
        return SimpleNamespace(returncode=0)

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
        patch("subprocess.run", side_effect=fake_dbt),
        patch("dango.utils.DbtLock"),
        patch(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
            return_value=False,
        ),
        patch("dango.transformation.build_finalize.finalize_dbt_build", fin),
    ):
        return CliRunner().invoke(run, [])


@pytest.fixture(autouse=True)
def _project_scaffold(tmp_path: Path) -> None:
    make_scaffold(tmp_path)


@pytest.mark.unit
def test_warned_test_is_reported_and_exit_is_zero(tmp_path: Path) -> None:
    result = _invoke(tmp_path, [("not_null_stg_a__t_x", "warn"), ("unique_stg_a__t_id", "pass")])
    assert result.exit_code == 0, result.output
    out = _plain(result.output)
    assert "the build succeeded. 1 data check(s) raised a warning" in out
    assert "not_null_stg_a__t_x" in out
    assert "unique_stg_a__t_id" not in out


@pytest.mark.unit
def test_no_note_when_nothing_warned(tmp_path: Path) -> None:
    result = _invoke(tmp_path, [("unique_stg_a__t_id", "pass")])
    assert result.exit_code == 0, result.output
    assert "raised a warning" not in _plain(result.output)
