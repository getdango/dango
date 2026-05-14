"""dango/web/routes/query.py

Ad-hoc SQL query execution against the DuckDB warehouse.

Security layers (defense-in-depth):
1. RBAC: ``require_permission("query.execute")`` — Editor + Admin only
2. Length limit: 100 KB max SQL
3. sqlglot whitelist: only ``exp.Select`` allowed
4. DuckDB ``config={"access_mode": "read_only"}``: rejects writes even if validation bypassed
5. Timeout: 30 s via ``asyncio.wait_for``
6. Row limit: 10 000 rows max
7. Audit logging: every query execution logged
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import duckdb
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.models import User
from dango.auth.permissions import require_permission
from dango.logging import get_logger
from dango.web.helpers import get_duckdb_path

logger = get_logger(__name__)

router = APIRouter(tags=["query"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_SQL_LENGTH = 102_400  # 100 KB
_MAX_ROWS = 10_000
_QUERY_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class QueryRequest(BaseModel):
    """Ad-hoc SQL query request."""

    sql: str = Field(max_length=102_400)


class QueryResponse(BaseModel):
    """Ad-hoc SQL query response."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    warning: str | None = None


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------


def _looks_like_select(sql: str) -> bool:
    """Return True if *sql* starts with SELECT or WITH (case-insensitive).

    Used as a basic keyword guard when sqlglot is unavailable or fails to
    parse.  Read-only access mode prevents database writes but does NOT prevent
    file-system writes (``COPY TO``, ``EXPORT DATABASE``), so we need this
    check as an additional layer.
    """
    first_keyword = sql.strip().split()[0].upper() if sql.strip() else ""
    return first_keyword in ("SELECT", "WITH")


def _validate_sql(sql: str) -> None:
    """Validate that *sql* is a single SELECT statement.

    Uses sqlglot for AST-level validation when available, falling back to a
    basic heuristic check otherwise.  Raises ``ValueError`` on rejection.
    """
    try:
        import sqlglot
        import sqlglot.expressions as exp
    except ImportError:
        # Fallback: keyword check + reject multi-statement
        if not _looks_like_select(sql):
            raise ValueError("Only SELECT queries are allowed") from None
        stripped = sql.strip().rstrip(";").strip()
        if ";" in stripped:
            raise ValueError("Multiple SQL statements are not allowed") from None
        return

    try:
        statements = sqlglot.parse(sql, dialect="duckdb")
    except sqlglot.errors.SqlglotError:
        # sqlglot could not parse — fall back to keyword check.
        # Read-only access mode is defense-in-depth for DB writes, but does not
        # prevent file-system writes (COPY TO, EXPORT), so we reject
        # anything that doesn't look like a SELECT.
        logger.warning("sqlglot_parse_failed", sql_length=len(sql))
        if not _looks_like_select(sql):
            raise ValueError("Only SELECT queries are allowed") from None
        raise ValueError("Invalid SQL syntax. Please check your query and try again.") from None
    except Exception:
        logger.warning("sqlglot_parse_failed", sql_length=len(sql))
        if not _looks_like_select(sql):
            raise ValueError("Only SELECT queries are allowed") from None
        raise ValueError("Invalid SQL syntax") from None

    # Filter out None entries (blank trailing semicolons)
    statements = [s for s in statements if s is not None]

    if len(statements) == 0:
        raise ValueError("Empty SQL statement")
    if len(statements) > 1:
        raise ValueError("Multiple SQL statements are not allowed")

    stmt = statements[0]
    if not isinstance(stmt, exp.Select):
        raise ValueError(f"Only SELECT queries are allowed, got {type(stmt).__name__}")


# ---------------------------------------------------------------------------
# Execution helper
# ---------------------------------------------------------------------------


def _execute_query(
    db_path: str,
    sql: str,
    max_rows: int,
) -> dict[str, Any]:
    """Execute a read-only SQL query against DuckDB.

    This is a blocking function intended to be called via
    ``asyncio.to_thread``.  Retries up to 3 times on ``duckdb.IOException``
    (e.g. when a sync holds the write lock) with exponential backoff.
    """
    last_exc: duckdb.IOException | None = None
    for attempt in range(3):
        try:
            conn = duckdb.connect(db_path, config={"access_mode": "read_only"})
            try:
                result = conn.execute(sql)
                columns = [desc[0] for desc in result.description]
                raw_rows = result.fetchmany(max_rows + 1)
                truncated = len(raw_rows) > max_rows
                rows = [list(r) for r in raw_rows[:max_rows]]
                return {
                    "columns": columns,
                    "rows": rows,
                    "row_count": len(rows),
                    "truncated": truncated,
                }
            finally:
                conn.close()
        except duckdb.IOException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.1 * (2**attempt))
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/api/query", response_model=None)
async def execute_query(
    body: QueryRequest,
    request: Request,
    user: User = Depends(require_permission("query.execute")),
) -> QueryResponse | JSONResponse:
    """Execute an ad-hoc SELECT query against the DuckDB warehouse."""
    sql = body.sql

    # -- Input validation ----------------------------------------------------
    if len(sql.strip()) == 0:
        return JSONResponse(
            status_code=400,
            content={"error_code": "DANGO-Q001", "message": "SQL query is empty"},
        )

    if len(sql) > _MAX_SQL_LENGTH:
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-Q001",
                "message": f"SQL query exceeds maximum length ({_MAX_SQL_LENGTH} bytes)",
            },
        )

    try:
        _validate_sql(sql)
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content={"error_code": "DANGO-Q002", "message": str(exc)},
        )

    # -- Warehouse check -----------------------------------------------------
    db_path = get_duckdb_path()
    if not db_path.exists():
        return JSONResponse(
            status_code=404,
            content={
                "error_code": "DANGO-Q003",
                "message": "Warehouse database not found. Run a sync first.",
            },
        )

    # -- Execute -------------------------------------------------------------
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_execute_query, str(db_path), sql, _MAX_ROWS),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=408,
            content={
                "error_code": "DANGO-Q004",
                "message": f"Query timed out after {_QUERY_TIMEOUT_SECONDS} seconds",
            },
        )
    except duckdb.Error as exc:
        logger.warning("query_execution_failed", error_type=type(exc).__name__)
        return JSONResponse(
            status_code=400,
            content={
                "error_code": "DANGO-Q005",
                "message": f"Query execution failed: {type(exc).__name__}",
            },
        )
    except Exception:
        logger.error("query_unexpected_error", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "error_code": "DANGO-Q006",
                "message": "An unexpected error occurred while executing the query",
            },
        )

    # -- Audit ---------------------------------------------------------------
    log_auth_event(
        AuditEvent.QUERY_EXECUTED,
        user_id=user.id,
        email=user.email,
        ip=request.client.host if request.client else None,
        details={
            "sql_length": len(sql),
            "row_count": result["row_count"],
            "truncated": result["truncated"],
        },
    )

    # -- Response ------------------------------------------------------------
    warning = None
    if result["truncated"]:
        warning = f"Results truncated to {_MAX_ROWS} rows"

    return QueryResponse(
        columns=result["columns"],
        rows=result["rows"],
        row_count=result["row_count"],
        truncated=result["truncated"],
        warning=warning,
    )
