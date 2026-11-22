"""tests/unit/test_sync_full_refresh_prompt.py

`dango sync --full-refresh` without `--yes` fails fast when nobody can answer its confirmation
prompt, and still waits for a person at a terminal.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

import dango
from dango.cli.commands.source import sync
from tests.unit.test_cli_sync import _patch_sync_prereqs

pytestmark = pytest.mark.unit

NEEDS = "--full-refresh needs confirmation"


def _invoke(args: list[str], *, input_text: str | None = None, interactive: bool | None = None):
    """Run ``sync`` through CliRunner with a mocked engine; returns (result, run_sync mock)."""
    run_sync = MagicMock(return_value={"failed_count": 0, "success_count": 1, "oauth_warnings": []})
    patches = [
        *_patch_sync_prereqs(),
        patch("dango.ingestion.run_sync", run_sync),
        patch("dango.visualization.metabase.sync_metabase_schema", return_value=False),
        patch("dango.oauth.validation.validate_before_sync"),
        patch("dango.cli.scaffold.enforce_scaffold"),
    ]
    if interactive is not None:
        patches.append(
            patch("dango.cli.utils.stdin_is_interactive", return_value=interactive, create=True)
        )
        patches.append(
            patch(
                "dango.cli.commands.source.stdin_is_interactive",
                return_value=interactive,
                create=True,
            )
        )
    for p in patches:
        p.start()
    try:
        result = CliRunner().invoke(sync, args, input=input_text, obj={"project_root": "/tmp"})
    finally:
        for p in patches:
            p.stop()
    return result, run_sync


class TestWithoutATerminal:
    def test_cli_runner_fails_fast_with_the_message_and_does_not_sync(self) -> None:
        result, run_sync = _invoke(["--full-refresh"])
        assert result.exit_code == 1, result.output
        assert NEEDS in result.output
        assert "--yes" in result.output
        run_sync.assert_not_called()

    def test_a_piped_answer_is_not_taken_for_a_destructive_prompt(self) -> None:
        result, run_sync = _invoke(["--full-refresh"], input_text="yes\nyes\n")
        assert result.exit_code == 1, result.output
        assert NEEDS in result.output
        run_sync.assert_not_called()

    def test_yes_flag_still_runs_without_a_terminal(self) -> None:
        result, run_sync = _invoke(["--full-refresh", "--yes"])
        assert result.exit_code == 0, result.output
        assert NEEDS not in result.output
        assert run_sync.call_args.kwargs["full_refresh"] is True

    def test_other_prompts_and_plain_sync_are_unchanged(self) -> None:
        result, run_sync = _invoke(["--yes"])
        assert result.exit_code == 0, result.output
        assert run_sync.call_args.kwargs["full_refresh"] is False


class TestAtATerminal:
    def test_the_prompt_is_shown_and_yes_continues(self) -> None:
        result, run_sync = _invoke(["--full-refresh"], input_text="yes\nyes\n", interactive=True)
        assert result.exit_code == 0, result.output
        assert "Full refresh will reload all data" in result.output
        assert NEEDS not in result.output
        assert run_sync.call_args.kwargs["full_refresh"] is True

    def test_no_aborts_without_syncing(self) -> None:
        result, run_sync = _invoke(["--full-refresh"], input_text="yes\nno\n", interactive=True)
        assert result.exit_code == 1
        assert NEEDS not in result.output
        run_sync.assert_not_called()


class TestFirstSyncScopePrompt:
    def test_without_a_terminal_it_is_not_asked_and_defaults_to_yes(self) -> None:
        result, run_sync = _invoke([])
        assert result.exit_code == 0, result.output
        assert "Continue?" not in result.output
        run_sync.assert_called_once()

    def test_a_piped_no_is_not_read_either(self) -> None:
        result, run_sync = _invoke([], input_text="no\n")
        assert "Continue?" not in result.output
        run_sync.assert_called_once()

    def test_at_a_terminal_it_still_asks(self) -> None:
        result, run_sync = _invoke([], input_text="no\n", interactive=True)
        assert "This will sync 1 source(s). Continue?" in result.output
        assert result.exit_code == 1
        run_sync.assert_not_called()

    def test_at_a_terminal_yes_continues(self) -> None:
        result, run_sync = _invoke([], input_text="yes\n", interactive=True)
        assert result.exit_code == 0, result.output
        run_sync.assert_called_once()


class _Stream:
    def __init__(self, tty: bool, closed: bool = False) -> None:
        self._tty, self.closed = tty, closed

    def isatty(self) -> bool:
        return self._tty


class TestStdinIsInteractive:
    @pytest.mark.parametrize(
        ("stream", "expected"),
        [
            (_Stream(True), True),
            (_Stream(False), False),
            (_Stream(True, closed=True), False),
            (None, False),
        ],
    )
    def test_decision(
        self, monkeypatch: pytest.MonkeyPatch, stream: object, expected: bool
    ) -> None:
        from dango.cli.utils import stdin_is_interactive

        monkeypatch.setattr(sys, "stdin", stream)
        assert stdin_is_interactive() is expected

    def test_a_stream_that_cannot_say_counts_as_not_interactive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.cli.utils import stdin_is_interactive

        class Broken:
            closed = False

            def isatty(self) -> bool:
                raise ValueError("I/O operation on closed file")

        monkeypatch.setattr(sys, "stdin", Broken())
        assert stdin_is_interactive() is False


def _project(root: Path, *, warehouse: bool = True) -> Path:
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True)
    project = {"project": {"name": "T", "created_by": "t@example.com", "purpose": "tests"}}
    (dango_dir / "project.yml").write_text(yaml.safe_dump(project), encoding="utf-8")
    sources = {
        "version": "1.0",
        "sources": [{"name": "csv_src", "type": "csv", "csv": {"directory": "data/csv"}}],
    }
    (dango_dir / "sources.yml").write_text(yaml.safe_dump(sources), encoding="utf-8")
    (root / "data" / "csv").mkdir(parents=True)
    if warehouse:  # without one this is a first sync and `dango sync` asks for the scope
        (root / "data" / "warehouse.duckdb").write_bytes(b"")
    (dango_dir / "auth.db").write_bytes(b"")  # a v1 project (not read as a v0.1 one)
    (root / "dbt").mkdir()
    for name in ("dbt_project.yml", "profiles.yml"):
        (root / "dbt" / name).write_text("{}\n", encoding="utf-8")
    return root


def _child_env(tmp_path: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["HOME"] = env["USERPROFILE"] = str(tmp_path / "home")
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(dango.__file__).resolve().parents[1]), env.get("PYTHONPATH", "")]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class TestRealCommandLine:
    """The real CLI in a child process with the stdin a script or agent would give it."""

    def _run(
        self,
        tmp_path: Path,
        stdin: int,
        args: tuple[str, ...] = ("--full-refresh",),
        *,
        warehouse: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        project = _project(tmp_path / "proj", warehouse=warehouse)
        cmd = [sys.executable, "-m", "dango.cli.main", "sync", *args]
        proc = subprocess.Popen(
            cmd,
            cwd=project,
            env=_child_env(tmp_path),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        try:
            # not communicate(): that would close the stdin pipe and hand the command an EOF
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            assert proc.stdout is not None
            pytest.fail(f"the command waited for an answer nobody can give:\n{proc.stdout.read()}")
        finally:
            if proc.stdin is not None:
                proc.stdin.close()
        assert proc.stdout is not None
        out = proc.stdout.read()
        proc.stdout.close()
        return subprocess.CompletedProcess(cmd, proc.returncode, out, ""), out

    def test_closed_stdin(self, tmp_path: Path) -> None:
        done, out = self._run(tmp_path, subprocess.DEVNULL)
        assert done.returncode == 1, out
        assert NEEDS in out and "--yes" in out

    def test_open_pipe_nobody_writes_to(self, tmp_path: Path) -> None:
        done, out = self._run(tmp_path, subprocess.PIPE)
        assert done.returncode == 1, out
        assert NEEDS in out and "--yes" in out

    def test_message_puts_yes_first_and_does_not_claim_there_is_no_terminal(
        self, tmp_path: Path
    ) -> None:
        done, out = self._run(tmp_path, subprocess.DEVNULL)
        assert done.returncode == 1, out
        flat = " ".join(out.split())
        assert flat.index("--yes") < flat.index("terminal")
        assert "run it in a terminal" not in flat
        assert "interactive terminal" in flat

    def test_first_sync_scope_prompt_does_not_wait_on_an_open_pipe(self, tmp_path: Path) -> None:
        """A fresh project asks "This will sync N source(s). Continue?"; no one can answer."""
        done, out = self._run(tmp_path, subprocess.PIPE, args=(), warehouse=False)
        assert "Continue?" not in out, out  # defaults to yes without asking, like EOF does
        assert done.returncode is not None  # it ended on its own: nothing waited for an answer
