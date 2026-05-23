"""dango/notebooks/templates/blank.py

Blank starter template — minimal DuckDB connection cell.

Available packages: pandas, duckdb, marimo, polars, pyarrow.
To install more: pip install <package> in the project's venv (source venv/bin/activate).
"""

import marimo

app = marimo.App()


@app.cell
def setup():
    """Connect to the local DuckDB warehouse in read-only mode."""
    import os

    import duckdb

    db_path = os.environ.get("DANGO_NOTEBOOK_DB_PATH", "data/warehouse.duckdb")
    conn = duckdb.connect(db_path, config={"access_mode": "read_only"})
    return (conn,)
