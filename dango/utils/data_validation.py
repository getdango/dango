"""
Data Validation Utilities

Provides validation for:
- Schema change detection
- Cursor field validation
- Data integrity checks
"""

import duckdb
from pathlib import Path
from typing import Dict, Any, List, Optional
from rich.console import Console
from datetime import datetime

console = Console()


def validate_cursor_field(
    duckdb_path: Path,
    source_name: str,
    cursor_field: str,
    schema: str = "raw"
) -> Dict[str, Any]:
    """
    Validate that cursor field exists and has correct format

    Args:
        duckdb_path: Path to DuckDB database
        source_name: Name of the source
        cursor_field: Name of cursor field (e.g., 'created_at', 'updated_at')
        schema: Schema name (default: 'raw')

    Returns:
        Dictionary with validation results:
        {
            "valid": bool,
            "exists": bool,
            "data_type": str,
            "sample_values": list,
            "issues": list
        }
    """
    result = {
        "valid": False,
        "exists": False,
        "data_type": None,
        "sample_values": [],
        "issues": []
    }

    try:
        conn = duckdb.connect(str(duckdb_path), read_only=True)

        # Check if table exists
        table_exists = conn.execute(f"""
            SELECT COUNT(*)
            FROM information_schema.tables
            WHERE table_schema='{schema}' AND table_name='{source_name}'
        """).fetchone()[0]

        if not table_exists:
            result["issues"].append(f"Table {schema}.{source_name} does not exist")
            conn.close()
            return result

        # Check if cursor field exists
        column_info = conn.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema='{schema}'
              AND table_name='{source_name}'
              AND column_name='{cursor_field}'
        """).fetchone()

        if not column_info:
            result["issues"].append(f"Cursor field '{cursor_field}' not found in table")
            result["exists"] = False
            conn.close()
            return result

        result["exists"] = True
        result["data_type"] = column_info[1]

        # Get sample values to verify format
        sample_query = f"""
            SELECT "{cursor_field}"
            FROM "{schema}"."{source_name}"
            WHERE "{cursor_field}" IS NOT NULL
            ORDER BY "{cursor_field}" DESC
            LIMIT 5
        """
        samples = conn.execute(sample_query).fetchall()
        result["sample_values"] = [str(row[0]) for row in samples]

        # Validate data type is appropriate for cursor
        valid_types = ['TIMESTAMP', 'DATE', 'INTEGER', 'BIGINT', 'VARCHAR']
        if not any(vtype in result["data_type"].upper() for vtype in valid_types):
            result["issues"].append(
                f"Cursor field has unexpected type '{result['data_type']}'. "
                f"Expected: {', '.join(valid_types)}"
            )

        # Check if cursor field has NULL values
        null_count = conn.execute(f"""
            SELECT COUNT(*)
            FROM "{schema}"."{source_name}"
            WHERE "{cursor_field}" IS NULL
        """).fetchone()[0]

        if null_count > 0:
            result["issues"].append(f"{null_count} NULL values found in cursor field")

        conn.close()

        # Overall validation
        result["valid"] = result["exists"] and len(result["issues"]) == 0

        return result

    except Exception as e:
        result["issues"].append(f"Validation error: {e}")
        return result


def detect_schema_changes(
    duckdb_path: Path,
    source_name: str,
    expected_schema: Optional[List[str]] = None,
    schema: str = "raw"
) -> Dict[str, Any]:
    """
    Detect schema changes in a source table

    Args:
        duckdb_path: Path to DuckDB database
        source_name: Name of the source
        expected_schema: Optional list of expected column names
        schema: Schema name (default: 'raw')

    Returns:
        Dictionary with schema change information:
        {
            "changed": bool,
            "current_columns": list,
            "added_columns": list,
            "removed_columns": list,
            "column_types": dict
        }
    """
    result = {
        "changed": False,
        "current_columns": [],
        "added_columns": [],
        "removed_columns": [],
        "column_types": {}
    }

    try:
        conn = duckdb.connect(str(duckdb_path), read_only=True)

        # Get current schema
        columns = conn.execute(f"""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema='{schema}' AND table_name='{source_name}'
            ORDER BY ordinal_position
        """).fetchall()

        result["current_columns"] = [col[0] for col in columns]
        result["column_types"] = {col[0]: col[1] for col in columns}

        # Compare with expected schema if provided
        if expected_schema:
            current_set = set(result["current_columns"])
            expected_set = set(expected_schema)

            result["added_columns"] = list(current_set - expected_set)
            result["removed_columns"] = list(expected_set - current_set)
            result["changed"] = len(result["added_columns"]) > 0 or len(result["removed_columns"]) > 0

        conn.close()
        return result

    except Exception as e:
        console.print(f"[yellow]⚠️  Schema detection error: {e}[/yellow]")
        return result


def validate_data_completeness(
    duckdb_path: Path,
    source_name: str,
    schema: str = "raw"
) -> Dict[str, Any]:
    """
    Validate data completeness and integrity

    Args:
        duckdb_path: Path to DuckDB database
        source_name: Name of the source
        schema: Schema name (default: 'raw')

    Returns:
        Dictionary with completeness metrics:
        {
            "row_count": int,
            "has_data": bool,
            "last_loaded": str,
            "health_score": str
        }
    """
    result = {
        "row_count": 0,
        "has_data": False,
        "last_loaded": None,
        "health_score": "unknown"
    }

    try:
        conn = duckdb.connect(str(duckdb_path), read_only=True)

        # Get row count
        count = conn.execute(f"""
            SELECT COUNT(*)
            FROM "{schema}"."{source_name}"
        """).fetchone()[0]

        result["row_count"] = count
        result["has_data"] = count > 0

        # Try to get last load time from dlt metadata
        try:
            last_load = conn.execute(f"""
                SELECT MAX(_dango_loaded_at)
                FROM "{schema}"."{source_name}"
            """).fetchone()

            if last_load and last_load[0]:
                result["last_loaded"] = str(last_load[0])
        except:
            pass

        # Determine health score
        if count == 0:
            result["health_score"] = "empty"
        elif count < 10:
            result["health_score"] = "very_low"
        elif count < 100:
            result["health_score"] = "low"
        elif count < 10000:
            result["health_score"] = "good"
        else:
            result["health_score"] = "excellent"

        conn.close()
        return result

    except Exception as e:
        console.print(f"[yellow]⚠️  Completeness check error: {e}[/yellow]")
        return result


def print_validation_report(
    source_name: str,
    cursor_validation: Dict[str, Any],
    schema_info: Dict[str, Any],
    completeness: Dict[str, Any]
):
    """
    Print a comprehensive validation report

    Args:
        source_name: Name of the source
        cursor_validation: Results from validate_cursor_field
        schema_info: Results from detect_schema_changes
        completeness: Results from validate_data_completeness
    """
    console.print(f"\n[bold]Validation Report: {source_name}[/bold]\n")

    # Data Completeness
    console.print("[bold cyan]Data Completeness:[/bold cyan]")
    console.print(f"  Rows: {completeness['row_count']:,}")
    console.print(f"  Health Score: {completeness['health_score']}")
    if completeness['last_loaded']:
        console.print(f"  Last Loaded: {completeness['last_loaded']}")

    # Schema Info
    console.print(f"\n[bold cyan]Schema:[/bold cyan]")
    console.print(f"  Columns: {len(schema_info['current_columns'])}")

    if schema_info['added_columns']:
        console.print(f"  [green]✅ Added columns: {', '.join(schema_info['added_columns'])}[/green]")

    if schema_info['removed_columns']:
        console.print(f"  [yellow]⚠️  Removed columns: {', '.join(schema_info['removed_columns'])}[/yellow]")

    # Cursor Validation
    if cursor_validation.get('exists'):
        console.print(f"\n[bold cyan]Cursor Field Validation:[/bold cyan]")
        console.print(f"  Field: ✅ Exists")
        console.print(f"  Type: {cursor_validation['data_type']}")

        if cursor_validation['sample_values']:
            console.print(f"  Sample: {cursor_validation['sample_values'][0]}")

    # Issues
    if cursor_validation.get('issues'):
        console.print(f"\n[bold yellow]⚠️  Issues:[/bold yellow]")
        for issue in cursor_validation['issues']:
            console.print(f"  • {issue}")

    console.print()
