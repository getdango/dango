"""dango/cli/db_helpers.py

Utilities for matching tables to source configurations, used by db status and db clean commands.

Re-exports from ``dango.utils.db_health`` for backwards compatibility.
"""

from dango.utils.db_health import build_schema_table_mapping, is_table_configured

__all__ = ["build_schema_table_mapping", "is_table_configured"]
