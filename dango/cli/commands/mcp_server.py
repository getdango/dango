"""dango/cli/commands/mcp_server.py

MCP server for Dango. Exposes Dango operations as typed tools for LLM agents.

Architecture: local stdio only. Spawned by LLM client config — users never
call `dango mcp` directly. Users run `dango mcp setup` once to configure
their LLM client.

Session E: read tools + setup
Session F: mutation tools (add_source, create_model, add_schedule, run_sync, run_transform)

CRITICAL: all *dango.* imports that touch real project/database/config state
are lazy (inside function bodies), never at module top level. The MCP server
communicates over stdio — any stray stdout write (a stray `print()`, an
eagerly-imported module that logs to stdout at import time, etc.) corrupts
the JSON-RPC protocol. Keep it that way. The one exception is the top-level
`from dango.cli.commands.mcp_helpers import ...` below: mcp_helpers.py itself
does zero dango.* imports at its own module level (stdlib only; all real
dango.* work stays lazy inside its function bodies), so importing it eagerly
here is inert and doesn't reintroduce the risk this rule guards against.
"""

from __future__ import annotations

import json
from typing import Any

import click
from fastmcp import FastMCP

from dango.cli.commands.mcp_helpers import (
    _connect_readonly_with_retry,
    _execute_query_sync,
    _get_project_root,
    _infer_layer,
    _validate_select_only,
)

mcp = FastMCP(
    "dango",
    instructions=(
        "Dango data platform MCP server. Use these tools to read catalog information, "
        "inspect schemas and lineage, query data, and trigger Dango operations. "
        "Always prefer these tools over direct file edits — they validate inputs and "
        "ensure consistency with Dango's conventions."
    ),
)

# Matches web/routes/query.py's QueryRequest.sql max_length / _DEFAULT_QUERY_TIMEOUT_SECONDS.
_MAX_QUERY_SQL_LENGTH = 102_400
_QUERY_TIMEOUT_SECONDS = 30


# ── Read tools ────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_sources() -> list[dict[str, Any]]:
    """List all configured data sources with their sync status and row counts.

    Returns a list of dicts with: name, type, enabled, last_sync, rows, status.
    """
    project_root = _get_project_root()
    import asyncio

    from dango.web.app import app as web_app
    from dango.web.helpers import get_source_status_data, load_sources_config

    # web/helpers.py resolves the project root via app.state.project_root
    # (normally set by lifespan() when the FastAPI server boots). The MCP
    # server never boots that app, so we set it here to reuse the same
    # status-aggregation code the web UI uses.
    web_app.state.project_root = project_root

    # This tool is a native async fastmcp tool (not a sync tool relying on
    # fastmcp's run_in_thread=True default to make asyncio.run() safe) — it
    # awaits directly on fastmcp's own event loop, same as
    # web/routes/sources.py does.
    sources_config = await asyncio.to_thread(load_sources_config)
    if not sources_config:
        return []

    tasks = [get_source_status_data(source) for source in sources_config]
    statuses = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for source, status in zip(sources_config, statuses, strict=True):
        if isinstance(status, BaseException):
            results.append(
                {
                    "name": source.get("name"),
                    "type": source.get("type"),
                    "enabled": source.get("enabled", True),
                    "last_sync": None,
                    "rows": None,
                    "status": "unknown",
                }
            )
            continue
        results.append(
            {
                "name": status.name,
                "type": status.type,
                "enabled": status.enabled,
                "last_sync": status.last_sync,
                "rows": status.row_count,
                "status": status.status,
            }
        )
    return results


@mcp.tool()
def get_table_schema(table_name: str, schema: str | None = None) -> dict[str, Any]:
    """Get the schema (columns, types, descriptions) for a table in the warehouse.

    Args:
        table_name: Table name (e.g. "stg_stripe__customers")
        schema: Schema name (e.g. "staging", "raw_stripe"). Auto-detected if omitted.

    Returns dict with: table_name, schema, columns (list of {name, type}).
    """
    project_root = _get_project_root()
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        return {"error": "No warehouse found. Run dango sync first."}

    try:
        conn = _connect_readonly_with_retry(db_path)
        try:
            if schema:
                result = conn.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = ? AND table_schema = ? ORDER BY ordinal_position",
                    [table_name, schema],
                ).fetchall()
            else:
                result = conn.execute(
                    "SELECT table_schema, column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = ? ORDER BY table_schema, ordinal_position",
                    [table_name],
                ).fetchall()
        finally:
            conn.close()

        if not result:
            return {"error": f"Table '{table_name}' not found in warehouse"}

        other_schemas: list[str] = []
        if not schema:
            # A table name can exist in more than one schema (e.g. a generic
            # name reused across two raw sources). Filter to the first
            # (alphabetically) schema's columns rather than silently merging
            # every matching table's columns into one list — that would
            # report a schema name whose columns don't actually match it.
            all_schemas = list(dict.fromkeys(r[0] for r in result))
            detected_schema = all_schemas[0]
            other_schemas = all_schemas[1:]
            result = [r for r in result if r[0] == detected_schema]
        else:
            detected_schema = schema

        columns = [{"name": r[-2], "type": r[-1]} for r in result]
        response = {"table_name": table_name, "schema": detected_schema, "columns": columns}
        if other_schemas:
            response["other_schemas"] = other_schemas
            response["note"] = (
                f"'{table_name}' also exists in {other_schemas}; showing '{detected_schema}'. "
                "Pass schema= to select a different one."
            )
        return response
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_catalog(source_filter: str | None = None) -> dict[str, Any]:
    """Get the data catalog: all tables grouped by schema with row counts.

    Args:
        source_filter: Optional source name to filter tables (e.g. "my_stripe_source")

    Returns dict with tables_by_schema (schema -> list of table names) and total count.
    """
    project_root = _get_project_root()
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        return {"error": "No warehouse found. Run dango sync first."}

    try:
        conn = _connect_readonly_with_retry(db_path)
        try:
            tables = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE' ORDER BY table_schema, table_name"
            ).fetchall()
        finally:
            conn.close()

        catalog: dict[str, list[str]] = {}
        for schema, tname in tables:
            if source_filter and source_filter not in schema and source_filter not in tname:
                continue
            catalog.setdefault(schema, []).append(tname)

        return {"tables_by_schema": catalog, "total": sum(len(v) for v in catalog.values())}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_lineage(model_name: str | None = None) -> dict[str, Any]:
    """Get dbt lineage from the manifest. Shows source -> staging -> intermediate -> mart flow.

    Args:
        model_name: Optional model to get lineage for. If omitted returns full DAG summary.

    Returns dict with nodes and edges representing the data lineage graph.
    """
    project_root = _get_project_root()
    manifest_path = project_root / "dbt" / "target" / "manifest.json"
    if not manifest_path.exists():
        return {"error": "No dbt manifest found. Run dango run first."}

    try:
        manifest = json.loads(manifest_path.read_text())
        nodes = manifest.get("nodes", {})
        sources = manifest.get("sources", {})

        if model_name:
            # Use the manifest's own dict key as the unique_id rather than
            # reconstructing "model.{package_name}.{model_name}" by hand —
            # that reconstruction breaks for versioned dbt models (dbt-core
            # 1.5+), whose real unique_id has a ".vN" suffix the manual
            # string wouldn't include, silently making referenced_by empty.
            target_entry = next(
                ((k, n) for k, n in nodes.items() if n.get("name") == model_name), None
            )
            if not target_entry:
                return {"error": f"Model '{model_name}' not found in manifest"}
            target_unique_id, target = target_entry
            return {
                "name": model_name,
                "path": target.get("original_file_path"),
                "depends_on": target.get("depends_on", {}).get("nodes", []),
                "referenced_by": [
                    k
                    for k, n in nodes.items()
                    if target_unique_id in n.get("depends_on", {}).get("nodes", [])
                ],
            }

        # Full summary
        return {
            "model_count": len([n for n in nodes.values() if n.get("resource_type") == "model"]),
            "source_count": len(sources),
            "models": [
                {"name": n["name"], "schema": n.get("schema"), "layer": _infer_layer(n["name"])}
                for n in nodes.values()
                if n.get("resource_type") == "model"
            ],
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def list_models() -> list[dict[str, Any]]:
    """List all dbt models with their layer, schema, and file path.

    Returns list of dicts with: name, layer, schema, path.
    """
    project_root = _get_project_root()
    manifest_path = project_root / "dbt" / "target" / "manifest.json"
    if not manifest_path.exists():
        return [{"error": "No manifest found. Run dango run first."}]

    try:
        manifest = json.loads(manifest_path.read_text())
        return [
            {
                "name": n["name"],
                "layer": _infer_layer(n["name"]),
                "schema": n.get("schema"),
                "path": n.get("original_file_path"),
            }
            for n in manifest.get("nodes", {}).values()
            if n.get("resource_type") == "model"
        ]
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
def get_model_sql(model_name: str) -> dict[str, Any]:
    """Get the SQL source for a dbt model.

    Args:
        model_name: Model name (e.g. "stg_stripe__customers")

    Returns dict with: name, sql, path.
    """
    project_root = _get_project_root()
    manifest_path = project_root / "dbt" / "target" / "manifest.json"
    if not manifest_path.exists():
        return {"error": "No manifest found. Run dango run first."}

    try:
        manifest = json.loads(manifest_path.read_text())
        node = next(
            (n for n in manifest.get("nodes", {}).values() if n.get("name") == model_name),
            None,
        )
        if not node:
            return {"error": f"Model '{model_name}' not found"}

        sql_path = project_root / "dbt" / node.get("original_file_path", "")
        sql = sql_path.read_text() if sql_path.exists() else node.get("raw_code", "")
        return {"name": model_name, "sql": sql, "path": str(sql_path)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def query(sql: str, row_limit: int = 500) -> dict[str, Any]:
    """Run a read-only SQL query against the DuckDB warehouse.

    Args:
        sql: SQL query to execute. Must be a single SELECT statement (WITH ... SELECT
            CTEs are allowed). Multi-statement SQL is rejected. Capped at
            102,400 characters (matches the web query endpoint).
        row_limit: Maximum rows to return (default 500, max 500).

    Returns dict with: columns, rows, row_count, truncated. Queries are killed
    after 30s (matching the web query endpoint's default) rather than blocking
    the server indefinitely on an expensive query.
    """
    if len(sql) > _MAX_QUERY_SQL_LENGTH:
        return {"error": f"SQL query too long (max {_MAX_QUERY_SQL_LENGTH} characters)"}

    try:
        _validate_select_only(sql)
    except ValueError as e:
        return {"error": str(e)}

    row_limit = min(row_limit, 500) if row_limit > 0 else 500

    project_root = _get_project_root()
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        return {"error": "No warehouse found. Run dango sync first."}

    import asyncio

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_execute_query_sync, db_path, sql, row_limit),
            timeout=_QUERY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return {"error": f"Query timed out after {_QUERY_TIMEOUT_SECONDS} seconds"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_sync_history(source_name: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Get recent sync history for a source or all sources.

    Args:
        source_name: Optional source name filter. If omitted, returns the most
            recent entries across all configured sources.
        limit: Number of recent records to return (default 10).

    Returns list of dicts with: source, status, rows_processed, duration_seconds,
    timestamp, full_refresh.
    """
    project_root = _get_project_root()
    from dango.utils.sync_history import load_sync_history

    try:
        if source_name:
            history = load_sync_history(project_root, source_name, limit)
            return [{**h, "source": source_name} for h in history]

        from dango.config.helpers import load_config

        config = load_config(project_root)
        combined: list[dict[str, Any]] = []
        for source in config.sources.sources:
            for h in load_sync_history(project_root, source.name, limit):
                combined.append({**h, "source": source.name})
        combined.sort(key=lambda h: h.get("timestamp") or "", reverse=True)
        return combined[:limit]
    except Exception as e:
        return [{"error": str(e)}]


# ── Setup + status commands ───────────────────────────────────────────────────


@click.group("mcp")
def mcp_group() -> None:
    """MCP server for AI coding agents (Claude Code, Cursor, Windsurf)."""


@mcp_group.command("run")
@click.pass_context
def mcp_run(ctx: click.Context) -> None:
    """Start the MCP stdio server. Called automatically by LLM clients — do not run manually."""
    # show_banner=False: fastmcp's default banner is harmless to the stdio
    # protocol (it renders to stderr), but showing it also triggers a
    # blocking network call to PyPI to check for a newer fastmcp version on
    # every server start. Dango's MCP server is spawned fresh by the LLM
    # client on every session — that's an unwanted, un-opt-in-able outbound
    # call on the local-stdio-only surface this session's egress gate exists
    # to protect. Suppress it.
    mcp.run(show_banner=False)


# ── Register subcommands from separate modules ─────────────────────────────────
# Mirrors the cross-file registration pattern in commands/remote.py: mcp_setup.py
# imports mcp_group from here and self-registers `setup`/`status` via
# @mcp_group.command(...) decorators. Split out to keep this file (the read
# tools + FastMCP server definition) under the 500-line file-size check.

import dango.cli.commands.mcp_setup as _mcp_setup  # noqa: E402, F401
