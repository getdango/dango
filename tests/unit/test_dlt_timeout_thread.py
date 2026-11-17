"""tests/unit/test_dlt_timeout_thread.py

DltPipelineRunner._run_with_timeout off the main thread (MCP worker threads) and on it (CLI).
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from dango.exceptions import SyncTimeoutError
from dango.ingestion.dlt_runner import DltPipelineRunner

pytestmark = pytest.mark.unit

_SIGALRM = 14  # value irrelevant: the signal module is spied, never armed for real


@pytest.fixture
def fake_alarm(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Spy on signal.signal/alarm (and provide SIGALRM/alarm where the OS has none)."""
    calls: list[Any] = []
    previous = object()
    monkeypatch.setattr(signal, "SIGALRM", _SIGALRM, raising=False)
    monkeypatch.setattr(signal, "alarm", lambda n: calls.append(("alarm", n)), raising=False)
    monkeypatch.setattr(
        signal, "signal", lambda sig, h: calls.append(("signal", sig, h)) or previous
    )
    calls.append(previous)
    return calls


def _run_in_thread(func: Any, minutes: int = 1) -> dict[str, Any]:
    """Run ``_run_with_timeout`` on a worker thread; return its result or exception."""
    out: dict[str, Any] = {}

    def target() -> None:
        try:
            out["result"] = DltPipelineRunner._run_with_timeout(MagicMock(), func, minutes)
        except Exception as exc:
            out["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(10)
    assert not thread.is_alive()
    return out


def test_worker_thread_runs_function_without_error() -> None:
    out = _run_in_thread(lambda: "ran")
    assert out == {"result": "ran"}


def test_worker_thread_passes_args_and_kwargs() -> None:
    out: dict[str, Any] = {}

    def target() -> None:
        out["r"] = DltPipelineRunner._run_with_timeout(MagicMock(), lambda a, b=0: a + b, 1, 2, b=3)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(10)
    assert out == {"r": 5}


def test_worker_thread_logs_not_enforced_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="dango.ingestion.dlt_runner"):
        _run_in_thread(lambda: "ran")
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("timeout not enforced when running off the main thread" in m for m in messages)


def test_worker_thread_does_not_touch_signal_handlers(fake_alarm: list[Any]) -> None:
    out = _run_in_thread(lambda: "ran")
    assert out == {"result": "ran"}
    assert fake_alarm[1:] == []


def test_worker_thread_propagates_function_exception() -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    out = _run_in_thread(boom)
    assert isinstance(out["error"], RuntimeError)
    assert str(out["error"]) == "boom"


def test_main_thread_arms_and_restores_alarm(fake_alarm: list[Any]) -> None:
    previous = fake_alarm[0]
    result = DltPipelineRunner._run_with_timeout(MagicMock(), lambda: "ok", 2)
    calls = fake_alarm[1:]

    assert result == "ok"
    assert calls[0][:2] == ("signal", _SIGALRM)
    assert callable(calls[0][2])
    assert calls[1] == ("alarm", 120)
    assert calls[2] == ("alarm", 0)
    assert calls[3] == ("alarm", 0)
    assert calls[4] == ("signal", _SIGALRM, previous)
    assert len(calls) == 5


def test_main_thread_handler_raises_sync_timeout(fake_alarm: list[Any]) -> None:
    DltPipelineRunner._run_with_timeout(MagicMock(), lambda: None, 3)
    handler = fake_alarm[1][2]
    with pytest.raises(SyncTimeoutError, match="3 minute"):
        handler(_SIGALRM, None)


def test_main_thread_no_off_thread_warning(
    fake_alarm: list[Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="dango.ingestion.dlt_runner"):
        DltPipelineRunner._run_with_timeout(MagicMock(), lambda: "ok", 1)
    assert not [r for r in caplog.records if "off the main thread" in r.getMessage()]
