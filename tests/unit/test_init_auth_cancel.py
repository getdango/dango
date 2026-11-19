"""tests/unit/test_init_auth_cancel.py

Ctrl+C or EOF at the `dango init` admin prompts cancels init and rolls back; real auth failures still skip auth.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from dango.cli.commands.project import init
from dango.cli.init import ProjectInitializer

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
TYPED_EMAIL = "me@example.com\nme@example.com\n"
SENTINELS = {"README.md": b"# mine\n", "notes/todo.txt": b"keep\n"}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    monkeypatch.delenv("DANGO_ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr("dango.utils.driver.reuse_verified_driver", lambda plugins_dir: True)
    monkeypatch.setattr(ProjectInitializer, "_generate_dbt_docs", lambda self: True)
    monkeypatch.setattr(
        "dango.cli.init.ProjectWizard.run",
        lambda self: ProjectInitializer(self.project_dir)._create_blank_config(),
    )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for rel, data in SENTINELS.items():
        (project / rel).parent.mkdir(parents=True, exist_ok=True)
        (project / rel).write_bytes(data)
    return project


def _tree(project: Path) -> set[str]:
    return {p.relative_to(project).as_posix() for p in project.rglob("*")}


def _run_init(project: Path, typed: str = "", args: tuple[str, ...] = ()) -> tuple[int, str]:
    """Run `dango init` with `typed` as stdin; running out of input is an EOF at the prompt."""
    result = CliRunner().invoke(init, [str(project), *args], input=typed)
    return result.exit_code, " ".join(_ANSI.sub("", result.output).split())


def _raw_init(project: Path, typed: str = "") -> tuple[int, str]:
    result = CliRunner().invoke(init, [str(project)], input=typed)
    return result.exit_code, _ANSI.sub("", result.output)


def _assert_clean_lines(raw: str) -> None:
    """No `Error:` prefix on its own line or in front of a progress line."""
    for line in raw.splitlines():
        assert line.strip() != "Error:", raw
    assert "Error:   Rolling back" not in raw, raw


def _assert_cancelled_and_rolled_back(project: Path, before: set[str], code: int, out: str) -> None:
    assert code == 1, out
    assert "Initialization cancelled by user" in out
    assert "Auth setup failed" not in out
    assert "Auth setup skipped" not in out
    assert _tree(project) == before
    for rel, data in SENTINELS.items():
        assert (project / rel).read_bytes() == data
    assert not (project / ".dango" / "auth.db").exists()
    assert not (project / ".dango" / "auth.yml").exists()


@pytest.mark.parametrize("typed", ["", "me@example.com\n"], ids=["email", "confirm_email"])
def test_eof_at_admin_prompt_rolls_back_and_exits_1(tmp_path: Path, typed: str) -> None:
    project = _project(tmp_path)
    before = _tree(project)

    code, output = _run_init(project, typed)

    _assert_cancelled_and_rolled_back(project, before, code, output)


@pytest.mark.parametrize("answered", [0, 1], ids=["email", "confirm_email"])
def test_ctrl_c_at_admin_prompt_rolls_back_and_exits_1(tmp_path: Path, answered: int) -> None:
    project = _project(tmp_path)
    before = _tree(project)
    # click.prompt raises click.Abort when the user presses Ctrl+C
    answers = ["me@example.com"] * answered + [click.Abort()]

    with patch("click.prompt", side_effect=answers):
        code, output = _run_init(project)

    _assert_cancelled_and_rolled_back(project, before, code, output)


def test_auth_failure_still_skips_auth_with_a_reason(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with patch("dango.auth.database.create_user", side_effect=RuntimeError("boom")):
        code, output = _run_init(project, TYPED_EMAIL)

    assert code == 0, output
    assert "Auth setup failed: boom" in output
    assert "Auth setup skipped" in output
    assert (project / ".dango" / "project.yml").exists()


def test_auth_failure_without_message_names_the_error_type(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with patch("dango.auth.database.create_user", side_effect=RuntimeError()):
        code, output = _run_init(project, TYPED_EMAIL)

    assert code == 0, output
    assert "Auth setup failed: RuntimeError" in output


def test_skip_wizard_never_prompts_and_still_creates_auth(tmp_path: Path) -> None:
    project = _project(tmp_path)

    code, output = _run_init(project, args=("--skip-wizard",))

    assert code == 0, output
    assert "Auth setup" not in output
    assert (project / ".dango" / "auth.db").exists()


def test_cancel_output_has_no_blank_error_lines(tmp_path: Path) -> None:
    project = _project(tmp_path)

    code, raw = _raw_init(project)

    assert code == 1, raw
    _assert_clean_lines(raw)
    assert "Error:" not in raw, raw  # a cancel is not an error (the prompt text precedes it)
    assert raw.count("Initialization cancelled by user") == 1
    assert raw.count("Rolling back...") == 1


def test_unexpected_error_output_names_the_reason(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with patch.object(ProjectInitializer, "_setup_auth", side_effect=ValueError("kaboom")):
        code, raw = _raw_init(project)

    assert code == 1, raw
    _assert_clean_lines(raw)
    assert "Unexpected error during initialization: kaboom" in raw
    assert raw.count("Rolling back...") == 1


def test_dbt_docs_failure_output_has_no_blank_error_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr(ProjectInitializer, "_generate_dbt_docs", lambda self: False)

    code, raw = _raw_init(project)

    assert code == 1, raw
    _assert_clean_lines(raw)
    assert raw.count("Initialization failed") == 1


def test_auth_failure_text_with_markup_is_shown_literally(tmp_path: Path) -> None:
    project = _project(tmp_path)

    with patch("dango.auth.database.create_user", side_effect=RuntimeError("[/x] boom")):
        code, output = _run_init(project, TYPED_EMAIL)

    assert code == 0, output
    assert "Auth setup failed: [/x] boom" in output
