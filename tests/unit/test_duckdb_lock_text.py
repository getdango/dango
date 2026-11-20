"""tests/unit/test_duckdb_lock_text.py

Recognising DuckDB's single-writer lock error and saying what it means in plain words.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from rich.console import Console

from tests.factories.duckdb_holder import make_warehouse, real_lock_error

pytestmark = pytest.mark.unit

_RAW_MARKERS = ("Could not set lock", "Conflicting lock", "duckdb.org", "IO Error")
_UNIX_TEXT = (
    'IO Error: Could not set lock on file "/data/warehouse.duckdb": Conflicting lock is held in '
    "/usr/bin/python3 (PID 4242) by user ed. See also https://example.invalid/concurrency"
)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def real_error(tmp_path: Path) -> tuple[Exception, int, Path]:
    db = make_warehouse(tmp_path / "data" / "warehouse.duckdb")
    err, pid = real_lock_error(db)
    return err, pid, db


class TestRecognition:
    def test_real_lock_error_is_recognised(self, real_error: tuple[Exception, int, Path]) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        assert is_duckdb_lock_error(real_error[0])

    def test_real_read_only_lock_error_is_recognised(self, tmp_path: Path) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        db = make_warehouse(tmp_path / "w.duckdb")
        err, _pid = real_lock_error(db, read_only=True)
        assert is_duckdb_lock_error(err)

    def test_wrapped_error_is_recognised(self, real_error: tuple[Exception, int, Path]) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        outer = RuntimeError("pipeline step failed")
        outer.__cause__ = real_error[0]
        assert is_duckdb_lock_error(outer)

    def test_unrelated_io_exception_is_not_a_lock(self, tmp_path: Path) -> None:
        from dango.utils.duckdb_lock_text import describe_error, is_duckdb_lock_error

        missing = tmp_path / "no-such-dir" / "w.duckdb"
        with pytest.raises(duckdb.IOException) as info:
            duckdb.connect(str(missing))
        assert not is_duckdb_lock_error(info.value)
        assert describe_error(info.value) == str(info.value)

    def test_unrelated_text_is_untouched(self) -> None:
        from dango.utils.duckdb_lock_text import rewrite_lock_text

        text = "IO Error: Cannot open file: No such file or directory (disk is full)"
        assert rewrite_lock_text(text) == text

    def test_windows_wording_is_recognised(self) -> None:
        # Hand-written: this is DuckDB's Windows wording; it was not reproduced on Windows here.
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        text = (
            'IO Error: Cannot open file "C:\\proj\\data\\warehouse.duckdb": The process cannot '
            "access the file because it is being used by another process."
        )
        assert is_duckdb_lock_error(RuntimeError(text))

    def test_other_file_in_use_is_not_a_warehouse_lock(self) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        text = (
            'IO Error: Cannot open file "C:\\proj\\notes.txt": The process cannot access the '
            "file because it is being used by another process."
        )
        assert not is_duckdb_lock_error(RuntimeError(text))


class TestMessage:
    def test_message_from_real_error(self, real_error: tuple[Exception, int, Path]) -> None:
        from dango.utils.duckdb_lock_text import describe_error

        err, pid, db = real_error
        text = describe_error(err)
        flat = _flat(text)
        assert "Another Dango process is using the warehouse" in flat
        assert "dango status" in flat
        assert "wait" in flat.lower()
        assert not any(marker in text for marker in _RAW_MARKERS)
        assert str(db) not in text
        assert f"PID {pid}" in flat
        text.encode("cp1252")  # plain ASCII-safe output

    def test_pid_is_shown_only_when_numeric(self) -> None:
        from dango.utils.duckdb_lock_text import describe_error

        shown = describe_error(RuntimeError(_UNIX_TEXT))
        assert "PID 4242" in shown
        hostile = _UNIX_TEXT.replace("(PID 4242)", "(PID 42[bold]x)")
        hidden = describe_error(RuntimeError(hostile))
        assert "PID" not in hidden
        assert "[bold]" not in hidden
        assert "Another Dango process" in hidden

    def test_text_around_the_lock_error_is_kept(self) -> None:
        from dango.utils.duckdb_lock_text import rewrite_lock_text

        text = f"Failed to check DuckDB health: {_UNIX_TEXT}"
        out = rewrite_lock_text(text)
        assert out.startswith("Failed to check DuckDB health: Another Dango process")
        assert "Conflicting lock" not in out

    def test_every_occurrence_is_rewritten(self) -> None:
        from dango.utils.duckdb_lock_text import rewrite_lock_text

        out = rewrite_lock_text(f"first: {_UNIX_TEXT}\nsecond: {_UNIX_TEXT}")
        assert out.count("Another Dango process") == 2
        assert "Could not set lock" not in out

    def test_hostile_text_renders_safely(self) -> None:
        from dango.utils.error_text import rich_error_text

        hostile = (
            'IO Error: Could not set lock on file "/x/[/x] [red]bad sk-live-abcdef123456": '
            "Conflicting lock is held in /bin/[bold]py (PID 7) by user [/]. See also https://e.invalid/x"
        )
        shown = rich_error_text(None, RuntimeError(hostile))
        buf = StringIO()
        Console(file=buf, force_terminal=False, width=200).print(
            shown
        )  # must not raise MarkupError
        out = buf.getvalue()
        assert "Another Dango process" in out
        assert "sk-live" not in out
        assert "[/x]" not in out


class TestPrintedAndLogged:
    def test_rich_error_text_rewrites_lock_text(
        self, real_error: tuple[Exception, int, Path]
    ) -> None:
        from dango.utils.error_text import rich_error_text

        shown = rich_error_text(None, real_error[0])
        assert "Another Dango process is using the warehouse" in _flat(shown)
        assert "Conflicting lock" not in shown

    def test_rich_error_text_leaves_other_errors_alone(self) -> None:
        from dango.utils.error_text import rich_error_text

        assert rich_error_text(None, RuntimeError("boom: connection refused")) == (
            "boom: connection refused"
        )

    def test_raw_text_reaches_the_debug_log_redacted(self, tmp_path: Path) -> None:
        from dango.utils.error_text import rich_error_text

        (tmp_path / ".env").write_text("API_TOKEN=supersecretvalue99\n", encoding="utf-8")
        raw = _UNIX_TEXT.replace("/data/", "/data/supersecretvalue99/")
        with patch("dango.utils.duckdb_lock_text.logger") as log:
            rich_error_text(tmp_path, RuntimeError(raw))
        logged = " ".join(str(a) for call in log.debug.call_args_list for a in call.args)
        assert "Could not set lock" in logged
        assert "supersecretvalue99" not in logged

    def test_describe_error_logs_raw_text(self) -> None:
        from dango.utils.duckdb_lock_text import describe_error

        with patch("dango.utils.duckdb_lock_text.logger") as log:
            describe_error(RuntimeError(_UNIX_TEXT))
        logged = " ".join(str(a) for call in log.debug.call_args_list for a in call.args)
        assert "Conflicting lock" in logged


def test_no_server_only_or_unix_only_imports() -> None:
    from dango.utils import duckdb_lock_text

    source = Path(duckdb_lock_text.__file__).read_text(encoding="utf-8")
    for banned in ("fcntl", "dango.platform.cloud", "dango.runtime", "termios", "import pwd"):
        assert banned not in source
    assert not any(ord(ch) > 127 for ch in source)


_OPEN_ONE = "IO Error: File is already open in \nC:\\Python312\\python.exe (PID 1234)"
_OPEN_TWO = (
    "IO Error: File is already open in \nC:\\Python312\\python.exe (PID 1234)"
    "\nC:\\Tools\\dbeaver.exe (PID 5678)"
)
_OPEN_NONE = "IO Error: File is already open in "


class TestWindowsAlreadyOpenWording:
    """DuckDB on Windows names the holding process (Restart Manager). Hand-written wording."""

    @pytest.mark.parametrize("text", [_OPEN_ONE, _OPEN_TWO, _OPEN_NONE])
    def test_recognised(self, text: str) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        assert is_duckdb_lock_error(duckdb.IOException(text))

    def test_pid_is_the_last_one_listed(self) -> None:
        from dango.utils.duckdb_lock_text import describe_error

        two = describe_error(RuntimeError(_OPEN_TWO))
        one = describe_error(RuntimeError(_OPEN_ONE))
        none = describe_error(RuntimeError(_OPEN_NONE))
        assert "Another Dango process" in two
        assert "(PID 5678)" in two
        assert "Another Dango process" in one
        assert "(PID 1234)" in one
        assert "Another Dango process" in none
        assert "PID" not in none

    def test_holder_lines_do_not_leak_and_surrounding_text_is_kept(self) -> None:
        from dango.utils.duckdb_lock_text import rewrite_lock_text

        out = rewrite_lock_text(f"Failed to check DuckDB health: {_OPEN_TWO} (retry later)")
        assert out.startswith("Failed to check DuckDB health: Another Dango process")
        assert out.endswith("(retry later)")
        assert "python.exe" not in out
        assert "dbeaver" not in out

    def test_the_older_windows_wording_still_works(self) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        text = (
            'IO Error: Cannot open file "C:\\p\\warehouse.duckdb": The process cannot access the '
            "file because it is being used by another process."
        )
        assert is_duckdb_lock_error(RuntimeError(text))

    def test_other_file_is_already_open_text_is_not_matched(self) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        assert not is_duckdb_lock_error(RuntimeError("IO Error: File is already open somewhere"))


class TestFollowContext:
    def test_implicit_context_is_ignored_when_asked(
        self, real_error: tuple[Exception, int, Path]
    ) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        try:
            try:
                raise real_error[0]
            except duckdb.IOException:
                raise NameError("a real bug while handling the lock error") from None
        except NameError as bug:
            assert is_duckdb_lock_error(bug)  # default: follows __context__
            assert not is_duckdb_lock_error(bug, follow_context=False)

    def test_explicit_cause_and_args_are_still_followed(
        self, real_error: tuple[Exception, int, Path]
    ) -> None:
        from dango.utils.duckdb_lock_text import is_duckdb_lock_error

        wrapped = RuntimeError("step failed")
        wrapped.__cause__ = real_error[0]
        assert is_duckdb_lock_error(wrapped, follow_context=False)
        assert is_duckdb_lock_error(
            RuntimeError("step failed", real_error[0]), follow_context=False
        )


def test_describe_error_redacts_secrets_before_the_debug_log(tmp_path: Path) -> None:
    from dango.utils.duckdb_lock_text import describe_error

    (tmp_path / ".env").write_text("API_TOKEN=supersecretvalue99\n", encoding="utf-8")
    raw = _UNIX_TEXT.replace("/data/", "/data/supersecretvalue99/")
    with patch("dango.utils.duckdb_lock_text.logger") as log:
        describe_error(RuntimeError(raw), tmp_path)
    logged = " ".join(str(a) for call in log.debug.call_args_list for a in call.args)
    assert "Could not set lock" in logged
    assert "supersecretvalue99" not in logged
