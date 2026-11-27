"""dango/ingestion/refresh_state.py

Incremental state for a staged `--full-refresh`: the staging pipeline extracted the whole source from
the initial value, so its final dlt state says where the refreshed data ends. The real pipeline must
adopt that state once the refreshed tables are swapped in, or its next plain sync re-reads (and, for
append tables, duplicates) every row between its old cursor and the end of the refresh.
"""

from __future__ import annotations

import copy
from typing import Any


def staged_source_state(staging_pipeline: Any) -> dict[str, Any]:
    """Copy of the source state (``{schema name: {"resources": {...}, ...}}``) of a staging pipeline.

    Read before the swap so a failure here leaves the destination untouched.
    """
    sources = staging_pipeline.state.get("sources") or {}
    return copy.deepcopy(dict(sources))


def adopt_staged_state(pipeline: Any, staged: dict[str, Any]) -> None:
    """Write the staged source state into *pipeline*'s local dlt state.

    Per schema, each resource the staging pipeline extracted gets its staged state as a whole (cursor
    value, boundary row hashes and any other resource state); resources it did not extract (a
    narrowed run) and other schemas keep theirs. Source-level keys of the staged state replace the
    real ones. dlt saves the state file only when the state changed.
    """
    if not staged:
        return
    with pipeline.managed_state() as state:
        sources = state.setdefault("sources", {})
        for schema_name, staged_source in staged.items():
            target = sources.setdefault(schema_name, {})
            for key, value in staged_source.items():
                if key == "resources" and isinstance(value, dict):
                    target.setdefault("resources", {}).update(copy.deepcopy(value))
                else:
                    target[key] = copy.deepcopy(value)
