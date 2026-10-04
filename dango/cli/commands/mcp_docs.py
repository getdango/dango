"""dango/cli/commands/mcp_docs.py

MCP documentation tools: table schemas with descriptions, model/source docs, coverage and dbt docs generation.
"""

from __future__ import annotations

import copy
import difflib
import re
from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import (
    _connect_readonly_with_retry,
    _get_project_root,
    _git_warnings,
)
from dango.cli.commands.mcp_server import mcp

# Copied verbatim from validate.py _check_description_completeness (a local tuple there).
_TODO_PATTERNS = ("# TODO", "TODO:", "todo:", "Add description", "add description")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _is_placeholder(desc: Any) -> bool:
    return bool(desc) and any(p in str(desc) for p in _TODO_PATTERNS)


def _is_described(desc: Any) -> bool:
    return bool(desc and str(desc).strip()) and not _is_placeholder(desc)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Lenient read: unreadable / invalid / non-mapping yml is treated as empty."""
    import yaml

    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _model_index(project_root: Path) -> dict[str, dict[str, Any]]:
    """model name -> docs entry, from every yml under dbt/models (read once)."""
    models_dir = project_root / "dbt" / "models"
    index: dict[str, dict[str, Any]] = {}
    if not models_dir.exists():
        return index
    for pattern in ("*.yml", "*.yaml"):
        for path in sorted(models_dir.rglob(pattern)):
            models = _read_yaml(path).get("models")
            for entry in models if isinstance(models, list) else []:
                if isinstance(entry, dict) and entry.get("name"):
                    index.setdefault(str(entry["name"]), entry)
    return index


def _columns_of(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
    cols = (entry or {}).get("columns")
    return (
        [c for c in cols if isinstance(c, dict) and c.get("name")] if isinstance(cols, list) else []
    )


def _tests_of(col: dict[str, Any]) -> list[Any]:
    """Column tests; dbt yml written by Dango templates uses ``tests:``, M5 writes ``data_tests:``."""
    tests = col.get("data_tests")
    if tests is None:
        tests = col.get("tests")
    return list(tests) if isinstance(tests, list) else []


def _shape(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "description": entry.get("description"),
        "columns": {
            str(c["name"]): {"description": c.get("description"), "data_tests": _tests_of(c)}
            for c in _columns_of(entry)
        },
    }


def _source_table_entry(project_root: Path, source: str, table: str) -> dict[str, Any] | None:
    path = project_root / "dbt" / "models" / "staging" / f"sources_{source}.yml"
    sources = _read_yaml(path).get("sources")
    for src in sources if isinstance(sources, list) else []:
        tables = src.get("tables") if isinstance(src, dict) else None
        for t in tables if isinstance(tables, list) else []:
            if isinstance(t, dict) and t.get("name") == table:
                return t
    return None


def _docs_for_relation(project_root: Path, schema: str, table: str) -> dict[str, Any] | None:
    """Docs for a warehouse relation: raw_<source> tables from sources_<source>.yml, else a model."""
    if schema.startswith("raw_"):
        return _shape(_source_table_entry(project_root, schema[len("raw_") :], table))
    from dango.transformation.model_common import find_model

    if find_model(project_root, table) is None:  # ModelServiceError (duplicate stem) propagates
        return None
    return _shape(_model_index(project_root).get(table))


def _warehouse_columns(project_root: Path) -> dict[str, dict[str, list[str]]] | None:
    """{table: {schema: [columns]}} from the warehouse (read-only); None if unavailable."""
    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        return None
    try:
        conn = _connect_readonly_with_retry(db_path)
        try:
            rows = conn.execute(
                "SELECT table_name, table_schema, column_name FROM information_schema.columns "
                "ORDER BY table_schema, table_name, ordinal_position"
            ).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - docs tools degrade to docs-only
        return None
    out: dict[str, dict[str, list[str]]] = {}
    for t, s, c in rows:
        out.setdefault(t, {}).setdefault(s, []).append(c)
    return out


def _pick_columns(
    warehouse: dict[str, dict[str, list[str]]] | None, table: str, layer: str
) -> list[str] | None:
    if warehouse is None:
        return None
    return warehouse.get(table, {}).get(
        layer
    )  # not built yet in its own layer -> unknown, never another schema's


def _suggest(name: str, names: list[str]) -> str:
    close = difflib.get_close_matches(name, names, n=3)
    return f" Did you mean: {', '.join(close)}?" if close else ""


# ── Tools ─────────────────────────────────────────────────────────────────────


@mcp.tool()
def get_table_schema(table_name: str, schema: str | None = None) -> dict[str, Any]:
    """Get the schema (columns, types, descriptions) for a table in the warehouse.

    Descriptions and data tests come from the dbt yml docs (model docs, or
    sources_<source>.yml for raw tables); undocumented items have description None.
    Edit docs with `update_model` (models) or `update_source_table_docs` (raw tables).

    Args:
        table_name: Table name (e.g. "stg_stripe__customers")
        schema: Schema name (e.g. "staging", "raw_stripe"). Auto-detected if omitted.

    Returns dict with: table_name, schema, description, columns (list of
    {name, type, description, data_tests}).
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

        docs, docs_error = None, None
        try:
            docs = _docs_for_relation(project_root, detected_schema, table_name)
        except Exception as e:  # noqa: BLE001 - e.g. duplicate model names; schema still useful
            docs_error = "; ".join(getattr(e, "errors", None) or [str(e)])
        doc_cols = (docs or {}).get("columns", {})
        columns = [
            {
                "name": r[-2],
                "type": r[-1],
                "description": doc_cols.get(r[-2], {}).get("description"),
                "data_tests": doc_cols.get(r[-2], {}).get("data_tests", []),
            }
            for r in result
        ]
        response: dict[str, Any] = {
            "table_name": table_name,
            "schema": detected_schema,
            "description": (docs or {}).get("description"),
            "columns": columns,
        }
        if docs_error:
            response["docs_error"] = docs_error
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
def get_model_docs(model_name: str) -> dict[str, Any]:
    """Get a dbt model's documentation and how it compares with the warehouse columns.

    Edit model/column docs with `update_model(description=..., columns=[...])`.

    Args:
        model_name: dbt model name (e.g. "fct_orders").

    Returns dict with: model_name, layer, docs_path, description, columns (list of
    {name, description, data_tests, in_warehouse (None if the warehouse/table is unavailable)}),
    undocumented_columns (warehouse columns with no docs entry).
    """
    project_root = _get_project_root()
    from dango.transformation.model_common import ModelServiceError, find_model, list_models
    from dango.transformation.model_docs import locate_model_docs

    try:
        found = find_model(project_root, model_name)
        if found is None:
            names = sorted(list_models(project_root))
            return {"error": f"Model '{model_name}' not found." + _suggest(model_name, names)}
        _, layer = found
        docs_file = locate_model_docs(project_root, model_name)
    except ModelServiceError as e:
        return {"error": "; ".join(e.errors), "errors": list(e.errors)}

    entry = _model_index(project_root).get(model_name)
    wh_cols = _pick_columns(_warehouse_columns(project_root), model_name, layer)
    documented = _columns_of(entry)
    columns = [
        {
            "name": str(c["name"]),
            "description": c.get("description"),
            "data_tests": _tests_of(c),
            "in_warehouse": None if wh_cols is None else c["name"] in wh_cols,
        }
        for c in documented
    ]
    doc_names = {str(c["name"]) for c in documented}
    return {
        "model_name": model_name,
        "layer": layer,
        "docs_path": str(docs_file.relative_to(project_root)) if docs_file else None,
        "description": (entry or {}).get("description"),
        "columns": columns,
        "undocumented_columns": [c for c in (wh_cols or []) if c not in doc_names],
    }


@mcp.tool()
def get_source_docs(source_name: str) -> dict[str, Any]:
    """Get documentation for a source and its raw tables.

    Two descriptions exist: `source_description` (.dango/sources.yml, edit with
    `update_source(description=...)`) and `dbt_source_description` (dbt sources_<source>.yml).
    Raw table and column docs (sources_<source>.yml) are edited with `update_source_table_docs`.

    Returns dict with: source_name, source_description, dbt_source_description, docs_path,
    tables (list of {name, description, columns: [{name, description, data_tests}]}).
    """
    project_root = _get_project_root()
    try:
        from dango.config.loader import ConfigLoader

        cfg = ConfigLoader(project_root).load_sources_config()
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not load .dango/sources.yml: {e}"}
    source = cfg.get_source(source_name)
    if source is None:
        names = [s.name for s in cfg.sources]
        return {"error": f"Source '{source_name}' not found." + _suggest(source_name, names)}

    path = project_root / "dbt" / "models" / "staging" / f"sources_{source_name}.yml"
    dbt_desc = None
    tables: list[dict[str, Any]] = []
    for src in (_read_yaml(path).get("sources") or []) if path.exists() else []:
        if not isinstance(src, dict):
            continue
        dbt_desc = dbt_desc if dbt_desc is not None else src.get("description")
        for t in src.get("tables") or []:
            if isinstance(t, dict) and t.get("name"):
                shaped = _shape(t) or {}
                cols = [{"name": n, **c} for n, c in shaped["columns"].items()]
                tables.append(
                    {"name": t["name"], "description": t.get("description"), "columns": cols}
                )
    return {
        "source_name": source_name,
        "source_description": source.description,
        "dbt_source_description": dbt_desc,
        "docs_path": str(path.relative_to(project_root)) if path.exists() else None,
        "tables": tables,
    }


@mcp.tool()
def update_source_table_docs(
    source_name: str,
    table: str,
    description: str | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Edit a raw table's docs in dbt/models/staging/sources_<source>.yml.

    Merge semantics match `update_model`: supplied keys override, columns merge by name,
    everything else (other keys, tables, top-level keys) is preserved. YAML comments are lost.
    The table must already exist in the file (created by sync).

    Args:
        source_name: Source name.
        table: Raw table name as listed in the file.
        description: New table description.
        columns: List of {"name", "description"?, "data_tests"?} entries.

    Returns dict with: status ("updated"/"unchanged"), path; or error.
    """
    project_root = _get_project_root()
    from dango.transformation.model_common import ModelServiceError
    from dango.transformation.model_docs import _load_yaml, _write_yaml

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", source_name) or ".." in source_name:
        return {"error": f"Invalid source name '{source_name}'"}
    if description is None and not columns:
        return {"error": "Nothing to update: pass description and/or columns"}
    if any(not isinstance(c, dict) or not c.get("name") for c in columns or []):
        return {"error": "every column entry must be a mapping with a 'name'"}
    path = project_root / "dbt" / "models" / "staging" / f"sources_{source_name}.yml"
    if not path.exists():
        return {
            "error": f"No docs file for source '{source_name}' (expected {path.name}). Sync it first."
        }
    try:
        data = _load_yaml(path)
    except ModelServiceError as e:
        return {"error": "; ".join(e.errors), "errors": list(e.errors)}

    original = copy.deepcopy(data)
    sources = data.get("sources")
    tbls = [
        t
        for src in (sources if isinstance(sources, list) else [])
        for t in ((src.get("tables") if isinstance(src, dict) else None) or [])
        if isinstance(t, dict)
    ]
    names = [str(t.get("name")) for t in tbls]
    entry = next((t for t in tbls if t.get("name") == table), None)
    if entry is None:
        return {"error": f"Table '{table}' not found in {path.name}." + _suggest(table, names)}
    if description is not None:
        entry["description"] = description
    if columns:
        existing = entry.get("columns")
        existing = existing if isinstance(existing, list) else []
        for col in columns:
            target = next(
                (c for c in existing if isinstance(c, dict) and c.get("name") == col["name"]), None
            )
            if target is None:
                existing.append(dict(col))
            else:
                if "data_tests" in col or "tests" in col:  # dbt rejects both keys on one column
                    target.pop("tests", None)
                    target.pop("data_tests", None)
                target.update(col)
        entry["columns"] = existing

    rel = str(path.relative_to(project_root))
    if data == original:
        return {"status": "unchanged", "path": rel}
    _write_yaml(path, data)
    result: dict[str, Any] = {"status": "updated", "path": rel}
    try:
        if warnings := _git_warnings(project_root):
            result["git_warning"] = warnings
    except Exception:  # noqa: BLE001 - the change already landed
        pass
    return result


@mcp.tool()
def docs_coverage(layer: str | None = None) -> dict[str, Any]:
    """Documentation coverage per dbt model (same placeholder rules as `dango validate`).

    A description is missing when empty or a TODO placeholder. `columns_total` covers the
    warehouse columns when the warehouse is readable, plus any documented extras.
    Fix gaps with `update_model(description=..., columns=[...])`.

    Args:
        layer: Optional filter: staging, intermediate or marts.

    Returns: models (model, layer, has_description, columns_total, columns_described,
    placeholders, undocumented_columns) and totals.
    """
    project_root = _get_project_root()
    from dango.transformation.model_common import list_models

    index = _model_index(project_root)
    warehouse = _warehouse_columns(project_root)
    rows: list[dict[str, Any]] = []
    for name, mlayer in sorted(list_models(project_root).items()):
        if layer and mlayer != layer:
            continue
        entry = index.get(name)
        desc = (entry or {}).get("description")
        doc_cols = {str(c["name"]): c.get("description") for c in _columns_of(entry)}
        wh = _pick_columns(warehouse, name, mlayer) or []
        all_cols = list(dict.fromkeys([*wh, *doc_cols]))
        described = [c for c in all_cols if _is_described(doc_cols.get(c))]
        placeholders = sum(1 for d in [desc, *doc_cols.values()] if _is_placeholder(d))
        rows.append(
            {
                "model": name,
                "layer": mlayer,
                "has_description": _is_described(desc),
                "columns_total": len(all_cols),
                "columns_described": len(described),
                "placeholders": placeholders,
                "undocumented_columns": [c for c in all_cols if c not in described],
            }
        )
    totals = {
        "models": len(rows),
        "models_described": sum(1 for r in rows if r["has_description"]),
        "columns_total": sum(r["columns_total"] for r in rows),
        "columns_described": sum(r["columns_described"] for r in rows),
        "placeholders": sum(r["placeholders"] for r in rows),
        "warehouse_checked": warehouse is not None,
    }
    return {"models": rows, "totals": totals}


@mcp.tool()
def generate_docs() -> dict[str, Any]:
    """Regenerate dbt docs (`dbt docs generate`). Holds the dbt lock; retry if the warehouse is busy.

    Returns dict with: status ("completed"/"failed"), output (ANSI-stripped tail).
    """
    project_root = _get_project_root()
    from dango.transformation import generate_dbt_docs
    from dango.utils import DbtLock, DbtLockError

    lock = DbtLock(project_root, source="mcp", operation="dbt docs generate")
    try:
        lock.acquire(timeout=30)
    except DbtLockError:
        return {"error": "Warehouse busy (sync or dbt running); retry shortly"}
    try:
        success, output = generate_dbt_docs(project_root)
    except Exception as e:  # noqa: BLE001
        return {"status": "failed", "output": f"{type(e).__name__}: {e}"}
    finally:
        if lock._acquired:
            lock.release()
    return {
        "status": "completed" if success else "failed",
        "output": _ANSI_RE.sub("", output)[-10_000:],
    }
