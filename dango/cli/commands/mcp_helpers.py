"""dango/cli/commands/mcp_helpers.py

Shared helpers for the MCP server's read tools (dango/cli/commands/mcp_server.py).
Split out purely to keep mcp_server.py under the file-size check — these are
plain functions, not Click-registered commands, so there's no cross-file
command-registration pattern involved (unlike mcp_setup.py), just a normal
helper extraction.

Safe to import at mcp_server.py's module top level despite the "lazy Dango
imports only" rule for that file: this module itself does zero dango.*
imports at its own top level (stdlib only below) — find_project_root,
duckdb, and sqlglot are all imported lazily inside the function bodies here,
same discipline as before the split. Importing *this* module is inert.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _get_project_root() -> Path:
    """Get project root for MCP tools. Raises RuntimeError if not in a project."""
    from dango.config.helpers import find_project_root

    try:
        return find_project_root()
    except Exception:
        raise RuntimeError(
            "Not inside a Dango project. cd into your project directory first."
        ) from None


def _infer_layer(model_name: str) -> str:
    if model_name.startswith("stg_"):
        return "staging"
    if model_name.startswith("int_"):
        return "intermediate"
    if model_name.startswith("fct_") or model_name.startswith("dim_"):
        return "marts"
    return "other"


def _validate_select_only(sql: str) -> None:
    """Validate that *sql* is a single SELECT (or WITH ... SELECT) statement.

    Uses sqlglot for AST-level validation when available (mirrors
    dango/web/routes/query.py's `_validate_sql`), falling back to a keyword +
    multi-statement heuristic otherwise. Raises ValueError on rejection.
    """
    if not sql.strip():
        raise ValueError("SQL query is empty")

    try:
        import sqlglot
        import sqlglot.expressions as exp
    except ImportError:
        first_keyword = sql.strip().split()[0].upper()
        if first_keyword not in ("SELECT", "WITH"):
            raise ValueError("Only SELECT queries are allowed") from None
        stripped = sql.strip().rstrip(";").strip()
        if ";" in stripped:
            raise ValueError("Multiple SQL statements are not allowed") from None
        return

    try:
        statements = [s for s in sqlglot.parse(sql, dialect="duckdb") if s is not None]
    except Exception as e:
        raise ValueError(f"Invalid SQL syntax: {e}") from None

    if len(statements) == 0:
        raise ValueError("Empty SQL statement")
    if len(statements) > 1:
        raise ValueError("Multiple SQL statements are not allowed")
    if not isinstance(statements[0], exp.Select):
        raise ValueError(f"Only SELECT queries are allowed, got {type(statements[0]).__name__}")


def _connect_readonly_with_retry(db_path: Path) -> Any:
    """Open a read-only DuckDB connection, retrying briefly on IOException.

    DuckDB is single-writer (see VAL-003 finding); a concurrent sync can
    transiently hold the write lock even though read-only connections are
    normally allowed alongside a writer. Mirrors the retry in
    dango/web/routes/query.py's `_execute_query`.
    """
    import time

    import duckdb

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return duckdb.connect(str(db_path), read_only=True)
        except duckdb.IOException as e:
            last_exc = e
            if attempt < 2:
                time.sleep(0.1 * (2**attempt))
    assert last_exc is not None
    raise last_exc


def _execute_query_sync(db_path: Path, sql: str, row_limit: int) -> dict[str, Any]:
    """Blocking DuckDB query execution. Run via asyncio.to_thread from query()."""
    conn = _connect_readonly_with_retry(db_path)
    try:
        rel = conn.execute(sql)
        columns = [d[0] for d in rel.description]
        raw_rows = rel.fetchmany(row_limit + 1)
        truncated = len(raw_rows) > row_limit
        rows = [list(r) for r in raw_rows[:row_limit]]
    finally:
        conn.close()
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }
