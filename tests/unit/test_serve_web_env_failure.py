"""tests/unit/test_serve_web_env_failure.py

`dango serve` and `dango web` stop with one clear message, and never a traceback, when the project
`.env` cannot be read. `dango serve` keeps `SystemExit(1)` with plain stderr text; its output for
every other failure is pinned here so it stays byte-identical.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.serve import serve
from dango.cli.commands.web import web
from dango.platform.common.startup_prologue import PrologueError

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_UTILS = "dango.cli.utils"
_PROLOGUE = "dango.platform.common.startup_prologue"
_CONFIG = "dango.config"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.delenv("DANGO_PROJECT_ROOT", raising=False)
    return root


def _squash(text: str) -> str:
    """Output without ANSI codes or line breaks (the console wraps long paths)."""
    return "".join(_ANSI.sub("", text).split())


def _serve(project_root: Path, *extra_patches: Any) -> tuple[Result, MagicMock]:
    loader = MagicMock()
    loader.load_config.side_effect = RuntimeError("config must not load")
    prologue = MagicMock()
    with (
        patch(f"{_UTILS}.require_project_context", return_value=project_root),
        patch(f"{_CONFIG}.ConfigLoader", return_value=loader),
        patch(f"{_PROLOGUE}.run_local_prologue", prologue),
    ):
        for extra in extra_patches:
            extra.start()
        try:
            result = CliRunner().invoke(serve, [])
        finally:
            for extra in reversed(extra_patches):
                extra.stop()
    return result, prologue


def _web(project_root: Path) -> tuple[Result, MagicMock, MagicMock]:
    prologue = MagicMock()
    uvicorn_run = MagicMock()
    with (
        patch(f"{_UTILS}.require_project_context", return_value=project_root),
        patch(f"{_PROLOGUE}.run_local_prologue", prologue),
        patch("uvicorn.run", uvicorn_run),
    ):
        result = CliRunner().invoke(web, [], obj={})
    return result, prologue, uvicorn_run


class TestServeUnreadableEnv:
    def test_non_utf8_env_stops_with_a_message_naming_the_file(self, project: Path) -> None:
        (project / ".env").write_bytes(b"caf\xe9=1\n")
        result, prologue = _serve(project)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert str(project / ".env") in result.stderr
        assert "UTF-8" in result.stderr
        assert "Traceback" not in result.output
        assert "UnicodeDecodeError" not in result.output
        prologue.assert_not_called()

    def test_message_matches_the_start_wording(self, project: Path) -> None:
        (project / ".env").write_bytes(b"caf\xe9=1\n")
        result, _ = _serve(project)
        assert result.stderr == (
            f"Cannot read {project / '.env'}: it must be UTF-8 text.\n"
            "Save the file as UTF-8, then run 'dango serve' again.\n"
        )

    def test_failure_comes_before_the_config_is_loaded(self, project: Path) -> None:
        (project / ".env").write_bytes(b"caf\xe9=1\n")
        result, _ = _serve(project)
        assert "config must not load" not in result.output
        assert "Failed to load project config" not in result.output

    def test_message_is_plain_ascii_text_on_stderr(self, project: Path) -> None:
        (project / ".env").write_bytes(b"caf\xe9=1\n")
        result, _ = _serve(project)
        assert result.stdout == ""
        assert "\x1b" not in result.stderr
        assert "[red]" not in result.stderr
        result.stderr.encode("cp1252")

    def test_nul_byte_env_stops_with_a_message_naming_the_file(self, project: Path) -> None:
        (project / ".env").write_bytes(b'KEY="a\x00b"\n')
        result, _ = _serve(project)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert str(project / ".env") in result.stderr
        assert "Traceback" not in result.output

    def test_unreadable_env_stops_with_the_reason(self, project: Path) -> None:
        (project / ".env").write_text("A=1\n", encoding="utf-8")
        denied = PermissionError(13, "Permission denied")
        result, _ = _serve(project, patch("dotenv.load_dotenv", side_effect=denied))
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)
        assert "Permission denied" in result.stderr
        assert str(project / ".env") in result.stderr

    def test_a_readable_env_still_lets_serve_continue(self, project: Path) -> None:
        (project / ".env").write_text("A=1\n", encoding="utf-8")
        result, prologue = _serve(project)
        # The fake config loader fails right after the env file loaded: serve got that far.
        assert "Failed to load project config: config must not load\n" in result.stderr
        prologue.assert_not_called()


class TestServeOutputForOtherFailuresIsUnchanged:
    """Byte-for-byte output of the failures serve already reported before this change."""

    def test_config_failure(self, project: Path) -> None:
        result, _ = _serve(project)
        assert result.exit_code == 1
        assert result.stderr == "Failed to load project config: config must not load\n"
        assert result.stdout == ""

    def test_prologue_failure(self, project: Path) -> None:
        good = MagicMock()
        good.load_config.return_value.project.name = "p"
        good.load_config.return_value.project.organization = None
        good.load_config.return_value.platform.port = 8800
        good.load_config.return_value.platform.workers = 1
        failure = PrologueError("DuckDB version mismatch", ValueError("driver pin differs"))
        with (
            patch(f"{_UTILS}.require_project_context", return_value=project),
            patch(f"{_CONFIG}.ConfigLoader", return_value=good),
            patch(f"{_PROLOGUE}.run_local_prologue", side_effect=failure),
        ):
            result = CliRunner().invoke(serve, [])
        assert result.exit_code == 1
        assert result.stderr == "DuckDB version mismatch: driver pin differs\n"
        assert result.stdout == ""

    def test_project_not_found(self, project: Path) -> None:
        import click

        with patch(f"{_UTILS}.require_project_context", side_effect=click.Abort()):
            result = CliRunner().invoke(serve, [])
        assert result.exit_code == 1
        assert result.stderr == ""
        assert result.stdout == ""


class TestWebUnreadableEnv:
    def test_non_utf8_env_aborts_with_a_message_naming_the_file(self, project: Path) -> None:
        (project / ".env").write_bytes(b"caf\xe9=1\n")
        result, prologue, uvicorn_run = _web(project)
        output = _squash(result.output)
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit)  # click.Abort, not a traceback
        assert _squash(str(project / ".env")) in output
        assert "UTF-8" in output
        assert "Traceback" not in output
        assert "UnicodeDecodeError" not in output
        assert "codec" not in output
        prologue.assert_not_called()
        uvicorn_run.assert_not_called()

    def test_message_matches_the_start_wording(self, project: Path) -> None:
        (project / ".env").write_bytes(b"caf\xe9=1\n")
        result, _, _ = _web(project)
        output = " ".join(_ANSI.sub("", result.output).split())
        assert "it must be UTF-8 text." in output
        assert "Save the file as UTF-8, then run 'dango web' again." in output

    def test_nul_byte_env_aborts_with_a_message_naming_the_file(self, project: Path) -> None:
        (project / ".env").write_bytes(b'KEY="a\x00b"\n')
        result, prologue, _ = _web(project)
        output = _squash(result.output)
        assert result.exit_code == 1
        assert _squash(str(project / ".env")) in output
        assert "Traceback" not in output
        prologue.assert_not_called()

    def test_unreadable_env_aborts_with_the_reason(self, project: Path) -> None:
        (project / ".env").write_text("A=1\n", encoding="utf-8")
        denied = PermissionError(13, "Permission denied")
        with patch("dotenv.load_dotenv", side_effect=denied):
            result, _, _ = _web(project)
        output = " ".join(_ANSI.sub("", result.output).split())
        assert result.exit_code == 1
        assert "Cannot read" in output
        assert "Permission denied" in output

    def test_message_escapes_rich_markup_in_the_path(self, tmp_path: Path) -> None:
        root = tmp_path / "[red]proj"
        root.mkdir()
        (root / ".env").write_bytes(b"caf\xe9=1\n")
        result, _, _ = _web(root)
        assert result.exit_code == 1
        assert "[red]proj" in _squash(result.output)

    def test_project_root_variable_is_not_left_behind(self, project: Path) -> None:
        import os

        (project / ".env").write_bytes(b"caf\xe9=1\n")
        _web(project)
        assert "DANGO_PROJECT_ROOT" not in os.environ
