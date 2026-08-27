"""dango/catalog/__init__.py

Standalone data-access layer for the data catalog — column schema, profiling,
lineage, and model/source browsing. No FastAPI dependency.
"""

from dango.catalog.lineage import _build_impact_response, _build_lineage_dag
from dango.catalog.manifest import _search_manifest
from dango.catalog.models import _build_catalog_models
from dango.catalog.schema import _get_column_schema

get_lineage = _build_lineage_dag
get_impact = _build_impact_response
get_models = _build_catalog_models
search_catalog = _search_manifest
get_column_schema = _get_column_schema

__all__ = [
    "get_lineage",
    "get_impact",
    "get_models",
    "search_catalog",
    "get_column_schema",
]
