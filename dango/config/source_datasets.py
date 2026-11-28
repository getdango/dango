"""dango/config/source_datasets.py

Which warehouse schemas and tables a configured source writes to, derived from the same values the
runner uses (`dlt_runner.py`: `_run_dlt_native_source`, `_get_dataset_name`, the file loaders).
Used by `dango db clean`, the MCP orphan report and `dango source remove`. Pure functions, no I/O;
local code that runs on Windows, macOS and Linux.
"""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from dango.config.models import DataSource, SourceType

__all__ = [
    "FILE_SOURCES",
    "claimed_schemas",
    "dataset_name_of",
    "dlt_dataset_name",
    "pipeline_name_of",
    "schemas_of",
    "staging_schema_of",
    "tables_of",
]

FILE_SOURCES = (SourceType.CSV, SourceType.LOCAL_FILES)


def pipeline_name_of(source: DataSource) -> str | None:
    """The dlt pipeline name the runner uses for *source* (None for file sources: no dlt state)."""
    if source.type in FILE_SOURCES:
        return None
    native = getattr(source, "dlt_native", None)
    override = (
        getattr(native, "pipeline_name", None) if source.type == SourceType.DLT_NATIVE else None
    )
    return str(override or source.name)


def dataset_name_of(source: DataSource) -> str:
    """The dataset name the runner passes for *source* (before dlt rewrites it)."""
    native = getattr(source, "dlt_native", None)
    override = (
        getattr(native, "dataset_name", None) if source.type == SourceType.DLT_NATIVE else None
    )
    return str(override or f"raw_{source.name}")


def _as_dlt_names(name: str) -> str:
    """The name dlt gives a dataset or table (snake case, e.g. ``Raw-Dash`` -> ``raw_dash``).

    A name dlt cannot normalise (it rejects blanks) comes back unchanged: such a pipeline cannot run.
    """
    try:
        from dlt.common.normalizers.naming.snake_case import NamingConvention

        return str(NamingConvention().normalize_table_identifier(name))
    except Exception:
        return name


def schemas_of(source: DataSource) -> list[str]:
    """Every schema *source* may write to, lower case, the most likely one first.

    dlt-backed sources claim the name as dlt rewrites it and as configured: claiming one name too
    many only keeps a schema, claiming one too few could drop live data. File sources write
    ``raw_<name>`` directly.
    """
    configured = dataset_name_of(source).casefold()
    if source.type in FILE_SOURCES:
        return [configured]
    rewritten = _as_dlt_names(dataset_name_of(source)).casefold()
    return [rewritten] if rewritten == configured else [rewritten, configured]


def dlt_dataset_name(name: str) -> str:
    """The schema dlt writes for the dataset *name* (``raw-dash`` -> ``raw_dash``, ``x_`` -> ``xx``).

    dlt keeps the configured name on the pipeline object (``pipeline.dataset_name``) and rewrites it
    only when it creates the schema: every query against the warehouse must use this name.
    """
    return _as_dlt_names(name)


def claimed_schemas(sources: Iterable[DataSource]) -> set[str]:
    """Lower-case schemas that any of *sources* (enabled or not) may write to."""
    return {schema for source in sources for schema in schemas_of(source)}


def staging_schema_of(source: DataSource) -> str | None:
    """Lower-case schema dlt writes the merge-staging tables of *source* into (None: file source).

    dlt names it ``<configured dataset>_staging`` and rewrites that like any dataset name.
    """
    if source.type in FILE_SOURCES:
        return None
    return _as_dlt_names(f"{dataset_name_of(source)}_staging").casefold()


def tables_of(source: DataSource) -> set[str] | None:
    """Tables the config names for *source* (lower case, as written and as dlt rewrites them).

    None means the source owns its whole schema: it lists no tables, or lists them in a form that
    does not give table names (for example endpoint blocks).
    """
    config = getattr(source, source.type.value, None)
    dumped = config.model_dump() if isinstance(config, BaseModel) else {}
    listed: Iterable[object] = (
        dumped.get("endpoints") or dumped.get("resources") or dumped.get("tables") or []
    )
    names: set[str] = set()
    for item in listed:
        if not isinstance(item, str):
            return None
        names |= {item.lower(), _as_dlt_names(item).lower()}
    return names or None
