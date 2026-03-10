"""dango/notebooks/templates/explore.py

Data exploration starter template — lists schemas/tables with sample queries.
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
def list_tables(conn):
    """List all user-created tables across schemas."""
    tables = conn.execute(
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
        "ORDER BY table_schema, table_name"
    ).fetchdf()
    return (tables,)


@app.cell
def sample_query(conn):
    """Run an ad-hoc query — edit the SQL below to explore your data."""
    # Edit the query below to explore your data
    result = conn.execute("SELECT 1 AS hello").fetchdf()
    return (result,)
