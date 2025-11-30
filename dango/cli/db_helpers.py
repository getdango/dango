"""
Database Helper Functions

Utilities for matching tables to source configurations, used by db status and db clean commands.
"""

from typing import Dict, Set, Tuple
from dango.config import DangoConfig
from dango.ingestion.sources.registry import get_source_metadata


def build_schema_table_mapping(config: DangoConfig) -> Tuple[Dict[str, Set[str]], Dict[str, str]]:
    """
    Build mapping of schemas to expected tables based on source configurations.

    For multi-resource sources (Stripe, Shopify, etc.):
      - Schema: raw_{source_name} (e.g., raw_stripe_test)
      - Tables: endpoint names (e.g., charge, customer, subscription)

    For single-resource sources (CSV, etc.):
      - Schema: raw
      - Tables: source names (e.g., orders)

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

        # Get source metadata to check if multi-resource
        metadata = get_source_metadata(source.type.value)
        is_multi_resource = metadata.get("multi_resource", False) if metadata else False

        if is_multi_resource:
            # Multi-resource source: one schema per source, tables are endpoint names
            schema_name = f"raw_{source_name}"
            source_to_schema[source_name] = schema_name

            # Get source-specific config
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
            # Single-resource source: 'raw' schema, table name is source name
            if 'raw' not in schema_to_tables:
                schema_to_tables['raw'] = set()
            schema_to_tables['raw'].add(source_name)
            source_to_schema[source_name] = 'raw'

    return schema_to_tables, source_to_schema


def is_table_configured(
    schema: str,
    table: str,
    schema_to_tables: Dict[str, Set[str]],
    source_to_schema: Dict[str, str]
) -> bool:
    """
    Check if a table is configured in sources.yml

    Args:
        schema: Table schema name
        table: Table name
        schema_to_tables: Mapping from schema to expected tables
        source_to_schema: Mapping from source name to schema

    Returns:
        True if table is configured, False if orphaned
    """
    # dlt internal tables are only configured if their schema belongs to an active source
    if table.startswith('_dlt_'):
        # For 'raw' schema, always keep _dlt_ tables (shared schema)
        if schema == 'raw':
            return True
        # For source-specific schemas (raw_{source_name}), check if schema is configured
        if schema.startswith('raw_'):
            return schema in schema_to_tables
        return True

    # Raw tables: check schema-specific expected tables
    if schema == 'raw' or schema.startswith('raw_'):
        expected_in_schema = schema_to_tables.get(schema, set())
        return table in expected_in_schema

    # Staging tables: stg_{source_name}__{endpoint} or stg_{source_name}
    elif schema == 'staging':
        if table.startswith('stg_'):
            # Try to match against source schemas
            for source_name, raw_schema in source_to_schema.items():
                # Check if staging table belongs to this source
                if table.startswith(f"stg_{source_name}__") or table == f"stg_{source_name}":
                    # Extract endpoint/table name
                    if "__" in table:
                        endpoint = table.split("__", 1)[1]
                    else:
                        endpoint = source_name

                    # Check if this endpoint exists in the raw schema
                    expected_in_raw = schema_to_tables.get(raw_schema, set())
                    if endpoint in expected_in_raw or source_name in expected_in_raw:
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
