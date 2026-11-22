"""dango/ingestion/interrupts.py

Turn an interrupt that dlt wrapped back into a ``KeyboardInterrupt``.

When Ctrl+C lands inside a dlt pipeline step (extract, normalize, load), dlt raises
``PipelineStepFailed`` with the ``KeyboardInterrupt`` inside it. Left alone, the runner would retry
the step or report the sync as failed. Level 1 module (``dlt`` and the standard library only).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

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
