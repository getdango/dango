"""dango/platform/common/__init__.py

Shared platform startup helpers used by both local and cloud startup flows.
"""

from .startup import (
    ensure_dbt_schemas,
    ensure_duckdb_driver,
    import_dashboards,
    run_pending_migrations,
    setup_metabase_if_needed,
    start_docker_services,
)

__all__ = [
    "run_pending_migrations",
    "ensure_dbt_schemas",
    "ensure_duckdb_driver",
    "start_docker_services",
    "setup_metabase_if_needed",
    "import_dashboards",
]
