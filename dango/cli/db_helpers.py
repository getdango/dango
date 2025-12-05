"""
Database Helper Functions

Utilities for matching tables to source configurations, used by db status and db clean commands.
"""

from typing import Dict, Set, Tuple
from dango.config import DangoConfig


def build_schema_table_mapping(config: DangoConfig) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    """
    Build mapping of schemas to expected tables based on source configurations.

    All sources use the pattern:
      - Schema: raw_{source_name} (e.g., raw_stripe_test, raw_orders)
      - Tables: endpoint/resource names from config

    This follows industry best practice (Airbyte, Fivetran) of one schema per source
    to prevent table name collisions.

    Args:
        config: Dango configuration with source definitions

    Returns:
        Tuple of:
          - schema_to_tables: Dict[schema_name, Set[table_names]]
          - source_to_schema: Dict[source_name, schema_name]
    """
    schema_to_tables = {}  # schema → set of table names
    source_to_schema = {}  # source_name → schema_name (for staging lookup)

    for source in config.sources.sources:
        source_name = source.name.lower()

        # All sources use raw_{source_name} schema pattern
        schema_name = f"raw_{source_name}"
        source_to_schema[source_name] = schema_name

        # Get source-specific config to find endpoints/resources/tables
        source_config = getattr(source, source.type.value, None)
        if source_config:
            source_dict = source_config.model_dump() if hasattr(source_config, 'model_dump') else {}
            endpoints = source_dict.get('endpoints') or source_dict.get('resources') or source_dict.get('tables')

            if endpoints:
                if schema_name not in schema_to_tables:
                    schema_to_tables[schema_name] = set()
                for endpoint in endpoints:
                    schema_to_tables[schema_name].add(endpoint.lower())
            else:
                # No explicit endpoints - schema will be discovered from DB
                if schema_name not in schema_to_tables:
                    schema_to_tables[schema_name] = set()

    return schema_to_tables, source_to_schema


def is_table_configured(
    schema: str,
    table: str,
    schema_to_tables: Dict[str, Set[str]],
    source_to_schema: Dict[str, str],
    actual_raw_tables: Dict[str, Set[str]] = None
) -> bool:
    """
    Check if a table is configured in sources.yml

    Args:
        schema: Table schema name
        table: Table name
        schema_to_tables: Mapping from schema to expected tables
        source_to_schema: Mapping from source name to schema
        actual_raw_tables: Optional dict of raw schema -> set of actual table names in DB

    Returns:
        True if table is configured, False if orphaned
    """
    # dlt internal tables are only configured if their schema belongs to an active source
    if table.startswith('_dlt_'):
        # For source-specific schemas (raw_{source_name}), check if schema is configured
        if schema.startswith('raw_'):
            return schema in schema_to_tables
        return True

    # Raw tables: check schema-specific expected tables
    if schema.startswith('raw_'):
        expected_in_schema = schema_to_tables.get(schema, set())
        # If no expected tables in mapping, the schema exists so table is valid
        # (tables are discovered from DB, not always pre-known)
        if not expected_in_schema:
            return schema in schema_to_tables
        return table in expected_in_schema

    # Staging tables: stg_{source_name}__{endpoint} or stg_{source_name}
    elif schema == 'staging':
        if table.startswith('stg_'):
            # Try to match against source schemas
            for source_name, raw_schema in source_to_schema.items():
                # Check if staging table belongs to this source
                if table.startswith(f"stg_{source_name}__"):
                    # Extract the raw table name from staging table
                    # stg_{source_name}__{table_name} -> table_name
                    prefix = f"stg_{source_name}__"
                    raw_table_name = table[len(prefix):]

                    # If we have actual raw tables, verify the corresponding raw table exists
                    if actual_raw_tables and raw_schema in actual_raw_tables:
                        return raw_table_name in actual_raw_tables[raw_schema]

                    # Fallback: source exists, so staging table is valid
                    return True
                elif table == f"stg_{source_name}":
                    # Single-table staging model
                    return True
            return False
        else:
            # Other staging tables - assume configured
            return True

    # Intermediate and marts tables - always assume configured
    # (these are custom models, not auto-generated)
    elif schema in ('intermediate', 'marts'):
        return True

    # Unknown schema - assume not configured
    return False
