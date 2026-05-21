"""dango/notebooks/templates/quality.py

Data quality starter template — null counts, distinct values, row counts.
"""

import marimo

app = marimo.App()


@app.cell
def guidance():
    """Introductory guidance for the quality notebook."""
    import marimo as mo

    return (
        mo.md(
            """
# Data Quality

This notebook shows **row counts** and **column metadata** for tables in your
DuckDB warehouse (read-only). Each cell's output appears below it — edit the
WHERE clause in `null_analysis` to inspect a specific table.

**Tip:** Use `mo.ui.table(df)` for interactive, sortable tables.
"""
        ),
    )


@app.cell
def setup():
    """Connect to the local DuckDB warehouse in read-only mode."""
    import os

    import duckdb

    db_path = os.environ.get("DANGO_NOTEBOOK_DB_PATH", "data/warehouse.duckdb")
    conn = duckdb.connect(db_path, config={"access_mode": "read_only"})
    return (conn,)


@app.cell
def row_counts(conn):
    """Compute row counts for every table in the warehouse."""
    tables = conn.execute(
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
        "ORDER BY table_schema, table_name"
    ).fetchall()
    counts = []
    for schema, table in tables:
        n = conn.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0]
        counts.append({"schema": schema, "table": table, "row_count": n})
    return (counts,)


@app.cell
def null_analysis(conn):
    """Show column metadata for a target table — edit the WHERE clause."""
    # Replace with your target table
    result = conn.sql(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'raw' "
        "ORDER BY ordinal_position "
        "LIMIT 20"
    ).fetchdf()
    return (result,)
