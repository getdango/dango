"""dango/ingestion/dlt_sources/_replay.py

Helper for the vendored connectors: wrap a generator-building call so dlt can run it again when a sync retries extraction.
"""

from collections.abc import Callable, Iterator
from typing import Any


def replayable(make: Callable[..., Any], *args: Any, **kwargs: Any) -> Callable[[], Iterator[Any]]:
    """Return a generator function that calls `make(*args, **kwargs)` afresh each time dlt runs it.

    A generator OBJECT passed to `dlt.resource` can be consumed only once: when the runner retries a failed
    extract it raises SourceExhausted and hides the real error. A generator function is called again per attempt.
    """

    def run() -> Iterator[Any]:
        yield from make(*args, **kwargs)

    return run
