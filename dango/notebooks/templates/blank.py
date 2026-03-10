"""dango/notebooks/templates/blank.py

Blank starter template — minimal DuckDB connection cell.
"""

import marimo

app = marimo.App()


@app.cell
def setup():
    """Connect to the local DuckDB warehouse in read-only mode."""
    import duckdb

    conn = duckdb.connect("data/warehouse.duckdb", read_only=True)
    return (conn,)
