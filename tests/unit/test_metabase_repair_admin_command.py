"""tests/unit/test_metabase_repair_admin_command.py

Verify `dango metabase repair-admin`: it forces the repair, maps outcomes to exit codes,
and refuses projects without a local docker-compose.yml.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.metabase_cmd import metabase

_REPAIR = "dango.platform.common.metabase_admin_repair.repair_admin_credential"


def _invoke(root: Path, result: dict[str, object]) -> tuple[int, str, list[object]]:
    with (
        patch("dango.cli.utils.require_project_context", return_value=root),
        patch(_REPAIR, return_value=result) as repair,
    ):
        out = CliRunner().invoke(metabase, ["repair-admin"], obj={})
    return out.exit_code, re.sub(r"\s+", " ", out.output), [repair.call_args]


@pytest.mark.unit
def test_command_passes_force_and_succeeds_on_repaired(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    code, output, calls = _invoke(tmp_path, {"status": "repaired"})

    assert code == 0, output
    assert calls[0].args == (tmp_path,)
    assert calls[0].kwargs == {"force": True}
    assert "no data is changed" in output
    assert "Metabase admin access restored." in output


@pytest.mark.unit
@pytest.mark.parametrize(
    "outcome",
    [
        {"status": "failed", "reason": "reset_cli_failed"},
        {"status": "skipped", "reason": "metabase_unreachable"},
    ],
)
def test_command_aborts_on_failed_or_skipped(tmp_path: Path, outcome: dict[str, object]) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    code, output, _calls = _invoke(tmp_path, outcome)

    assert code != 0
    assert "Metabase admin access restored." not in output


@pytest.mark.unit
def test_command_aborts_without_local_compose_file(tmp_path: Path) -> None:
    code, output, calls = _invoke(tmp_path, {"status": "repaired"})

    assert code != 0
    assert "local projects" in output
    assert calls == [None]
