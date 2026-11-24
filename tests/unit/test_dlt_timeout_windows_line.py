"""tests/unit/test_dlt_timeout_windows_line.py

DltPipelineRunner._run_with_timeout on a platform without SIGALRM (Windows): no per-sync console line.
"""

from __future__ import annotations

import logging
import signal
from unittest.mock import MagicMock, patch

import pytest

from dango.ingestion.dlt_runner import DltPipelineRunner

pytestmark = pytest.mark.unit

_LOGGER = "dango.ingestion.dlt_runner"


@pytest.fixture
def no_alarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove SIGALRM and alarm from the signal module, as on Windows (restored by monkeypatch)."""
    for name in ("SIGALRM", "alarm"):
        monkeypatch.delattr(signal, name, raising=False)


def test_without_alarm_runs_function_and_prints_nothing(
    no_alarm: None, capsys: pytest.CaptureFixture[str]
) -> None:
    with patch("dango.ingestion.dlt_runner.console") as console:
        result = DltPipelineRunner._run_with_timeout(MagicMock(), lambda a, b=0: a + b, 1, 2, b=3)

    assert result == 5
    console.print.assert_not_called()
    captured = capsys.readouterr()
    assert "Windows" not in captured.out + captured.err
    assert "without timeout" not in captured.out + captured.err


def test_without_alarm_real_console_stays_silent(
    no_alarm: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert DltPipelineRunner._run_with_timeout(MagicMock(), lambda: "ok", 1) == "ok"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_without_alarm_emits_a_debug_record(
    no_alarm: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        DltPipelineRunner._run_with_timeout(MagicMock(), lambda: "ok", 1)

    records = [
        r
        for r in caplog.records
        if r.name == _LOGGER and "timeout not enforced on this platform" in r.getMessage()
    ]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG


def test_with_alarm_present_emits_no_platform_record(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER), patch("signal.alarm"):
        DltPipelineRunner._run_with_timeout(MagicMock(), lambda: "ok", 1)

    assert not [r for r in caplog.records if "not enforced on this platform" in r.getMessage()]
