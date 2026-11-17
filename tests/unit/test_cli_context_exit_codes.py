"""tests/unit/test_cli_context_exit_codes.py

Commands that need a project or a file and do not find it exit 1, not 0.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BLANK_ERROR = re.compile(r"^\s*Error:\s*$", re.MULTILINE)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")


def _invoke(argv: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli, argv)
    return result.exit_code, _ANSI.sub("", result.output)


def _assert_fails(argv: list[str], message: str) -> None:
    code, output = _invoke(argv)
    assert code == 1, output
    assert message in output
    assert not _BLANK_ERROR.search(output), output


@pytest.mark.parametrize("argv", [["source", "add"], ["source", "edit"]])
def test_source_add_and_edit_outside_project_exit_1(argv: list[str]) -> None:
    _assert_fails(argv, "Not in a dango project directory")


def _make_project(root: Path, sources_yml: str | None) -> None:
    dango = root / ".dango"
    dango.mkdir()
    (dango / "project.yml").write_text("project:\n  name: t\n", encoding="utf-8")
    if sources_yml is not None:
        (dango / "sources.yml").write_text(sources_yml, encoding="utf-8")


def test_source_edit_without_sources_yml_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _make_project(root, None)
    monkeypatch.chdir(root)
    _assert_fails(["source", "edit"], "No sources.yml found")


def test_source_edit_unknown_source_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _make_project(root, "sources:\n  - name: chess\n    type: csv\n")
    monkeypatch.chdir(root)
    _assert_fails(["source", "edit", "nope"], "Source 'nope' not found")


def test_source_edit_invalid_yaml_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _make_project(root, "sources: []\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr("click.edit", lambda text, **kw: "sources: [unclosed\n")
    _assert_fails(["source", "edit"], "Changes NOT saved")


def test_source_edit_unchanged_file_still_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    _make_project(root, "sources: []\n")
    monkeypatch.chdir(root)
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr("click.edit", lambda text, **kw: text)
    code, output = _invoke(["source", "edit"])
    assert code == 0, output
    assert "No changes detected" in output


def _schedule_project(root: Path) -> Path:
    root.mkdir()
    _make_project(root, "sources: []\n")
    (root / ".dango" / "schedules.yml").write_text("schedules: []\n", encoding="utf-8")
    return root


def _schedule_add(
    root: Path, answers: list[dict[str, str]], confirm: bool = False
) -> tuple[int, str]:
    with (
        patch("inquirer.prompt", side_effect=answers),
        patch("dango.cli.utils.find_project_root", return_value=root),
        patch("dango.web.routes.scripts_helpers._discover_scripts", return_value=[]),
        patch("dango.cli.commands.schedule.safe_confirm", return_value=confirm),
    ):
        return _invoke(["schedule", "add"])


def test_schedule_add_sync_without_sources_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _schedule_project(tmp_path / "proj")
    monkeypatch.chdir(root)
    code, output = _schedule_add(
        root, [{"name": "nightly"}, {"type": "Sync & Transform (recommended)"}]
    )
    assert code == 1, output
    assert "Sync schedules require at least one source" in output
    assert not _BLANK_ERROR.search(output), output


def test_schedule_add_script_without_scripts_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _schedule_project(tmp_path / "proj")
    monkeypatch.chdir(root)
    code, output = _schedule_add(
        root, [{"name": "report"}, {"type": "Script (custom Python)"}], confirm=False
    )
    assert code == 1, output
    assert "No Python scripts found" in output
    assert not _BLANK_ERROR.search(output), output


def test_schedule_add_script_that_already_exists_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _schedule_project(tmp_path / "proj")
    (root / "scripts").mkdir()
    (root / "scripts" / "my_report.py").write_text("print(1)\n", encoding="utf-8")
    monkeypatch.chdir(root)
    code, output = _schedule_add(
        root,
        [{"name": "report"}, {"type": "Script (custom Python)"}, {"script_name": "my_report.py"}],
        confirm=True,
    )
    assert code == 1, output
    assert "already exists" in output
    assert (root / "scripts" / "my_report.py").read_text(encoding="utf-8") == "print(1)\n"
