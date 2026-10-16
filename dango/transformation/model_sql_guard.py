"""dango/transformation/model_sql_guard.py

Fail-closed check that agent/service-written dbt model SQL reads only warehouse tables (no files, URLs, settings, env).
"""

import ast
import re
from typing import Any

from dango.transformation.model_sql_functions import (
    function_error,
    is_denied_function,
    load_function_allowlist,
)

__all__ = ["FILE_EXTENSIONS", "check_model_sql", "is_denied_function", "load_function_allowlist"]

_NEXT_STEP = (
    "Use ref()/source() on a warehouse table; load files with `dango source add` "
    "(local_files) instead."
)
_UNVERIFIABLE = "Could not verify this SQL safely; simplify it"

# Jinja: comments first so their contents are never inspected as code
_JINJA_RE = re.compile(r"\{#.*?#\}|\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_REF_RE = re.compile(
    r"ref\(\s*(['\"])([A-Za-z0-9_\-]+)\1\s*(?:,\s*(['\"])([A-Za-z0-9_\-]+)\3\s*)?"
    r"(?:,\s*(?:v|version)\s*=\s*(?:\d+|(['\"])[A-Za-z0-9_.\-]+\5)\s*)?\)"
)
_SOURCE_RE = re.compile(
    r"source\(\s*(['\"])([A-Za-z0-9_.\-]+)\1\s*,\s*(['\"])([A-Za-z0-9_.\-]+)\3\s*\)"
)
_CONFIG_RE = re.compile(r"config\((.*)\)", re.DOTALL)
_ALLOWED_STATEMENTS = {"if is_incremental()", "else", "endif"}
_CONFIG_KEYS = {
    "materialized",
    "schema",
    "alias",
    "tags",
    "unique_key",
    "incremental_strategy",
    "on_schema_change",
    "enabled",
}
_MATERIALIZATIONS = {"table", "view", "incremental", "ephemeral"}
_JINJA_LITERALS = {"true": True, "false": False, "none": None}

FILE_EXTENSIONS = frozenset(
    "csv tsv tab txt json jsonl ndjson parquet pq gz zst db duckdb sqlite sqlite3 "
    "xlsx xls shp geojson gpkg arrow avro orc".split()
)
_PLACEHOLDER = "__dango_ref_"


def _literal(node: ast.expr) -> bool:
    """True for a constant, a Jinja true/false/none, or a list/tuple of those."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return node.id.lower() in _JINJA_LITERALS
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_literal(e) for e in node.elts)
    return False


def _check_config(args: str) -> list[str]:
    """config(): keyword arguments only, allow-listed keys, literal values."""
    bad = f"config({args.strip()})"
    try:
        call = ast.parse(f"f({args})", mode="eval").body
    except SyntaxError:
        return [f"Jinja construct not allowed in agent-created models: {bad}"]
    if not isinstance(call, ast.Call) or call.args:
        return [f"Jinja construct not allowed in agent-created models: {bad}"]
    errors: list[str] = []
    for kw in call.keywords:
        if kw.arg not in _CONFIG_KEYS or not _literal(kw.value):
            errors.append(
                f"config({kw.arg or '**'}=...) is not allowed in agent-created models; "
                f"allowed keys with literal values: {', '.join(sorted(_CONFIG_KEYS))}"
            )
        elif kw.arg == "materialized" and not (
            isinstance(kw.value, ast.Constant) and kw.value.value in _MATERIALIZATIONS
        ):
            errors.append(
                f"config(materialized=...) must be one of: {', '.join(sorted(_MATERIALIZATIONS))}"
            )
    return errors


def _strip_jinja(sql: str) -> tuple[str, list[str]]:
    """Replace allowed Jinja with placeholders/blanks; return (plain SQL, errors)."""
    errors: list[str] = []
    counter = 0

    def repl(m: re.Match[str]) -> str:
        """Map one Jinja block to a placeholder or blank, recording disallowed blocks."""
        nonlocal counter
        block = m.group(0)
        if block.startswith("{#"):
            return " "
        inner = block[2:-2].strip("-+").strip()
        if block.startswith("{%"):
            if " ".join(inner.split()) not in _ALLOWED_STATEMENTS:
                errors.append(f"Jinja construct not allowed in agent-created models: {block}")
            return " "
        if inner == "this" or _REF_RE.fullmatch(inner) or _SOURCE_RE.fullmatch(inner):
            counter += 1
            return f" {_PLACEHOLDER}{counter} "
        cfg = _CONFIG_RE.fullmatch(inner)
        if cfg:
            errors.extend(_check_config(cfg.group(1)))
            return " "
        errors.append(f"Jinja construct not allowed in agent-created models: {block}")
        return " "

    plain = _JINJA_RE.sub(repl, sql)
    if not errors and any(t in plain for t in ("{{", "{%", "{#")):
        errors.append("Jinja construct not allowed in agent-created models: unclosed Jinja block")
    return plain, errors


def _func_name(node: object) -> str:
    from sqlglot import exp

    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return type(node).__name__.lower()


def _check_table(table: Any) -> str | None:
    """Row-source allow-list for one exp.Table; returns an error or None."""
    from sqlglot import exp

    inner = table.this
    if isinstance(inner, (exp.GenerateSeries, exp.Unnest, exp.Explode)):
        return None
    if not isinstance(inner, exp.Identifier):
        return f"{_func_name(inner)}() is not allowed as a row source in agent-created models. {_NEXT_STEP}"
    parts = [p.name for p in table.parts]
    shown = ".".join(parts)
    if any(c in part for part in parts for c in "/\\:.") or not all(parts):
        return f"'{shown}' looks like a file path, not a warehouse table. {_NEXT_STEP}"
    if len(parts) > 1 and parts[-1].lower() in FILE_EXTENSIONS:
        return (
            f"'{shown}' ends in a file extension (.{parts[-1]}) and DuckDB would read it as "
            f"a file. {_NEXT_STEP}"
        )
    return None


def check_model_sql(sql: str) -> list[str]:
    """Return errors for model SQL that could read files/URLs/settings or run non-query SQL."""
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError

    plain, errors = _strip_jinja(sql)
    if errors:
        return errors
    try:
        statements = [s for s in sqlglot.parse(plain, dialect="duckdb") if s is not None]
    except SqlglotError:
        return [_UNVERIFIABLE]
    if len(statements) != 1:
        return ["Model SQL must be exactly one SELECT query (no other statements)"]
    root = statements[0]
    blocked = (exp.Command, exp.Copy, exp.DML, exp.DDL, exp.Set, exp.Pragma, exp.Attach)
    if not isinstance(root, (exp.Select, exp.SetOperation, exp.Pivot)) or any(
        isinstance(n, blocked) for n in root.walk()
    ):
        return [
            f"Model SQL must be a SELECT query; {root.key.upper()} statements are not allowed "
            "in agent-created models"
        ]
    for node in root.walk():
        err: str | None = None
        if isinstance(node, exp.Table):
            err = _check_table(node)
        elif isinstance(node, exp.Lateral) and not isinstance(
            node.this, (exp.Subquery, exp.Select, exp.Unnest, exp.Explode)
        ):
            err = f"{_func_name(node.this)}() is not allowed as a row source in agent-created models. {_NEXT_STEP}"
        elif isinstance(node, exp.Func):
            err = function_error(node, _NEXT_STEP)
        if err and err not in errors:
            errors.append(err)
    return errors
