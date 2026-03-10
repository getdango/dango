"""dango/notebooks/templates/quality.py

Data quality starter template — null counts, distinct values, row counts.
"""

import marimo

app = marimo.App()


@app.cell
def setup():
    """Connect to the local DuckDB warehouse in read-only mode."""
    import duckdb

    conn = duckdb.connect("data/warehouse.duckdb", read_only=True)
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
    import pandas as pd

    counts_df = pd.DataFrame(counts)
    return (counts_df,)


@app.cell
def null_analysis(conn):
    """Show column metadata for a target table — edit the WHERE clause."""
    # Replace with your target table
    result = conn.execute(
        "SELECT column_name, data_type "
        "FROM information_schema.columns "
        "WHERE table_schema = 'raw' "
        "ORDER BY ordinal_position "
        "LIMIT 20"
    ).fetchdf()
    return (result,)
