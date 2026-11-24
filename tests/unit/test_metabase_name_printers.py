"""tests/unit/test_metabase_name_printers.py

Names that come from Metabase (dashboards, questions, collections, skip reasons, repair actions)
must print literally in `dango metabase save` and `load` without changing exit codes, and a secret
that straddles the 500-character cut of one error item must still be redacted.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from dango.cli.commands.metabase_cmd import metabase_load, metabase_save
from dango.cli.commands.platform import print_collection_repair
from tests.unit.test_metabase_error_printers import (
    ALL_SITES,
    _console,
    _errors_for,
    _flat,
    _run,
    _run_manager_command,
    _text,
)
from tests.unit.test_start_metabase_setup_failure_message import _failed, _run_start

# A name Rich would reject (unmatched closing tag) and one it would silently swallow (a tag).
NAMES = ["Q1 [/draft] board", "Sales [beta] board"]


def _saved(name: str) -> dict[str, Any]:
    entry = {"name": name, "type": "question", "collection": f"Coll {name}"}
    return {
        "success": True,
        "errors": [],
        "exported_dashboards": [{"name": name, "cards": 2, "collection": f"Coll {name}"}],
        "exported_questions": [entry],
        "exported_models": [entry],
        "exported_metrics": [entry],
        "exported_timelines": [entry],
        "skipped_collections": [f"Personal {name}"],
    }


def _loaded(name: str) -> dict[str, Any]:
    entry = {"name": name, "type": f"kind {name}", "reason": f"reason {name}"}
    return {
        "success": True,
        "errors": [],
        "imported_dashboards": [{"name": name, "cards": 2}],
        "imported_questions": [entry],
        "imported_models": [entry],
        "imported_metrics": [entry],
        "imported_timelines": [entry],
        "skipped": [entry],
        "would_overwrite": [entry],
    }


@pytest.mark.unit
@pytest.mark.parametrize("name", NAMES)
def test_save_prints_metabase_names_literally(tmp_path: Path, name: str) -> None:
    console = _console(io.StringIO())
    code = _run_manager_command(metabase_save, "save_to_files", _saved(name), tmp_path, console)

    out = _flat(_text(console))
    assert code == 0, out
    assert out.count(name) == 5  # dashboard, question, model, metric, timeline
    assert f"Coll {name}" in out
    assert f"Personal {name}" in out
    assert "Next steps" in out


@pytest.mark.unit
@pytest.mark.parametrize("name", NAMES)
def test_load_prints_metabase_names_literally(tmp_path: Path, name: str) -> None:
    from unittest.mock import MagicMock, patch

    console = _console(io.StringIO())
    manager = MagicMock()
    manager.load_from_files.return_value = _loaded(name)
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.visualization.dashboard_manager.DashboardManager", return_value=manager),
        patch("dango.cli.commands.metabase_cmd.console", console),
    ):
        code = CliRunner().invoke(metabase_load, ["--dry-run"], obj={}).exit_code

    out = _flat(_text(console))
    assert code == 0, out
    assert f"? {name} (2 cards)" in out
    assert out.count(name) >= 8  # five imported, one skipped, one would-overwrite, plus the card
    assert f"kind {name}" in out
    assert f"reason {name}" in out
    assert "To actually import" in out


@pytest.mark.unit
@pytest.mark.parametrize("name", NAMES)
def test_collection_repair_actions_print_literally(name: str) -> None:
    console = _console(io.StringIO())
    action = f"create collection '{name}'"

    print_collection_repair(console, {"collection_repair": {"planned": [action]}})

    assert f"Would {action}" in _flat(_text(console))


@pytest.mark.unit
@pytest.mark.parametrize("name", NAMES)
def test_start_prints_repair_actions_literally_and_exits_zero(tmp_path: Path, name: str) -> None:
    console = _console(io.StringIO())
    action = f"create collection '{name}'"
    result, _ = _run_start(
        tmp_path,
        _failed(True, []),
        console,
        import_result={"imported": [], "collection_repair": {"planned": [action]}},
    )

    assert result.exit_code == 0, result.output
    assert f"Would {action}" in _flat(_text(console))


# Secrets that straddle character 500 of one item: redaction must run before the cut.
_URL_PASSWORD = "hunter2secret-abcdefghijkl"
_ENV_VALUE = "plum-orbit-55821"


@pytest.mark.unit
@pytest.mark.parametrize("site", ALL_SITES)
@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("x" * 480 + f" http://admin:{_URL_PASSWORD}@h/ tail", _URL_PASSWORD),
        ("x" * 494 + f" {_ENV_VALUE} tail", _ENV_VALUE),
    ],
)
def test_secret_across_the_cut_point_is_redacted(
    tmp_path: Path, site: str, text: str, secret: str
) -> None:
    (tmp_path / ".env").write_text(f"MY_SECRET_VALUE={_ENV_VALUE}\n", encoding="utf-8")

    code, out = _run(site, tmp_path, _errors_for(site, [text]))

    assert code == ALL_SITES[site][1], out
    assert "xxxx" in out
    assert secret[:4] not in out
