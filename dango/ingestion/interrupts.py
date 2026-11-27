"""dango/ingestion/interrupts.py

Turn an interrupt that dlt or DuckDB wrapped back into a ``KeyboardInterrupt``.

When Ctrl+C lands inside a dlt pipeline step (extract, normalize, load), dlt raises
``PipelineStepFailed`` with the ``KeyboardInterrupt`` inside it. Left alone, the runner would retry
the step or report the sync as failed. DuckDB does the same for a query: a signal that arrives
during a query comes back as ``duckdb.InterruptException`` or as a ``RuntimeError`` whose cause is
the ``KeyboardInterrupt``. Level 1 module (``dlt``, ``duckdb`` and the standard library only).
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import duckdb
from dlt.pipeline.exceptions import PipelineStepFailed

_MAX_DEPTH = 6


def wraps_interrupt(error: BaseException) -> bool:
    """True when *error* is, or was raised because of, a ``KeyboardInterrupt``."""
    stack: list[tuple[BaseException, int]] = [(error, 0)]
    seen: set[int] = set()
    while stack:
        current, depth = stack.pop()
        if id(current) in seen or depth > _MAX_DEPTH:
            continue
        seen.add(id(current))
        if isinstance(current, KeyboardInterrupt):
            return True
        inner = getattr(current, "exception", None)
        for candidate in (inner, current.__cause__, current.__context__):
            if isinstance(candidate, BaseException):
                stack.append((candidate, depth + 1))
    return False


@contextmanager
def interrupts_stay_interrupts() -> Iterator[None]:
    """Re-raise a dlt-wrapped interrupt as ``KeyboardInterrupt``; other errors pass unchanged."""
    try:
        yield
    except PipelineStepFailed as exc:
        if wraps_interrupt(exc):
            raise KeyboardInterrupt from exc
        raise


def is_interrupt(error: BaseException) -> bool:
    """True when *error* is DuckDB's or dlt's report of an interrupt (Ctrl+C, SIGTERM)."""
    return isinstance(error, duckdb.InterruptException) or wraps_interrupt(error)


def reraise_if_interrupt(error: BaseException) -> None:
    """Raise ``KeyboardInterrupt`` (from *error*) when *error* reports an interrupt.

    Call it first in an ``except Exception`` that wraps DuckDB work, so a Ctrl+C is not counted
    as a failed file or a schema mismatch.
    """
    if is_interrupt(error):
        raise KeyboardInterrupt from error


def reraise_current_if_interrupt() -> None:
    """:func:`reraise_if_interrupt` for the exception being handled (call inside ``except``)."""
    error = sys.exc_info()[1]
    if error is not None:
        reraise_if_interrupt(error)


def _raise_keyboard_interrupt(signum: int, frame: object) -> None:
    raise KeyboardInterrupt


@contextmanager
def sigterm_as_interrupt() -> Iterator[None]:
    """Make SIGTERM raise ``KeyboardInterrupt`` while the block runs, like Ctrl+C does.

    Without it a SIGTERM (stop, cancel, shutdown) ends the process on the spot and no cleanup
    runs. Only the default disposition is replaced: a handler somebody else installed stays, and
    off the main thread (where ``signal.signal`` is not allowed) nothing changes. On Windows a
    SIGTERM sent to a process ends it without running a handler, so this has no effect there.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)
    if previous != signal.SIG_DFL:
        yield
        return
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)
