"""dango/ingestion/staging_cleanup.py

Which dlt staging schemas (`<dataset>_staging`) the end of a sync may drop. A schema a configured
source loads into is never dropped: a source called `a_staging` writes `raw_a_staging`, the same
name dlt uses for the merge-staging tables of source `a`. Local code: runs on Windows, macOS and
Linux.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from dango.config.loader import ConfigLoader
from dango.config.models import DataSource
from dango.config.source_datasets import claimed_schemas
from dango.logging import get_logger

logger = get_logger(__name__)

__all__ = ["staging_schemas_to_drop"]


def _claimed_by_configuration(project_root: Path, given: Iterable[DataSource]) -> set[str] | None:
    """Schemas the configured sources and the sources of this sync may write to (None: unknown)."""
    try:
        configured = ConfigLoader(project_root).load_sources_config().sources
    except Exception as exc:
        logger.debug("staging_cleanup_config_unreadable", error_type=type(exc).__name__)
        return None
    return claimed_schemas([*configured, *given])


def staging_schemas_to_drop(
    schemas: Iterable[str],
    succeeded: Iterable[str],
    project_root: Path,
    given: Iterable[DataSource],
) -> list[str]:
    """The `*_staging` schemas of *schemas* that belong to a source that synced and that no source claims.

    A schema qualifies when its name without `_staging` (and without a `raw_` prefix) is a source that
    succeeded; it is kept when any configured source, enabled or not, or any of the sources *given*
    to this sync may write to it. When the configuration cannot be read nothing is dropped.
    """
    done = set(succeeded)
    candidates = [
        name
        for name in schemas
        if (base := name.removesuffix("_staging")) in done or base.removeprefix("raw_") in done
    ]
    if not candidates:
        return []
    claimed = _claimed_by_configuration(project_root, given)
    if claimed is None:
        return []
    kept = [name for name in candidates if name.casefold() in claimed]
    if kept:
        logger.debug("staging_schema_kept_a_source_loads_into_it", schemas=kept)
    return [name for name in candidates if name.casefold() not in claimed]
