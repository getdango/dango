"""dango/notebooks/templates/explore.py

Data exploration starter template — lists schemas/tables with sample queries.
"""

import marimo

app = marimo.App()


@app.cell
def guidance():
    """Introductory guidance for the explore notebook."""
    import marimo as mo

    return (
        mo.md(
            """
# Data Exploration

This notebook is connected to your DuckDB warehouse in **read-only** mode.
Each cell's output appears below it — edit the SQL in `sample_query` to explore
your data.

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
def list_tables(conn):
    """List all user-created tables across schemas."""
    tables = conn.sql(
        "SELECT table_schema, table_name "
        "FROM information_schema.tables "
        "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
        "ORDER BY table_schema, table_name"
    )
    return (tables,)


@app.cell
def sample_query(conn):
    """Run an ad-hoc query — edit the SQL below to explore your data."""
    # Edit the query below to explore your data
    result = conn.sql("SELECT 1 AS hello")
    return (result,)
