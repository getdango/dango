"""dango/transformation/model_sql.py

SQL analysis and rendering for dbt models: ref/source extraction, lineage validation, file templates.
"""

import difflib
import re
from datetime import datetime
from pathlib import Path

from dango.transformation.model_common import (
    CUSTOM_MODEL_LAYERS,
    ModelServiceError,
    iter_model_files,
    list_models,
    stems,
)

_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_BLOCK_RE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
# ref('x') / ref("x") / ref('pkg', 'x') (target = last positional string) / ref('x', v=2)
_REF_RE = re.compile(r"\bref\s*\(\s*(['\"])([^'\"]+)\1\s*(?:,\s*(['\"])([^'\"]+)\3\s*)?[,)]")
# source('src', 'tbl')
_SOURCE_RE = re.compile(r"\bsource\s*\(\s*(['\"])([^'\"]+)\1\s*,\s*(['\"])([^'\"]+)\3\s*[,)]")
_RAW_TABLE_RE = re.compile(r"\braw_[a-z0-9_]+\.[a-z0-9_]+", re.IGNORECASE)
_CONFIG_RE = re.compile(r"\bconfig\s*\(")


def _jinja_blocks(sql: str) -> list[str]:
    return _JINJA_BLOCK_RE.findall(_JINJA_COMMENT_RE.sub("", sql))


def extract_refs(sql: str) -> tuple[list[str], list[str]]:
    """Return (ref targets, "source.table" names) found in Jinja blocks, de-duplicated."""
    refs: list[str] = []
    sources: list[str] = []
    for block in _jinja_blocks(sql):
        for m in _REF_RE.finditer(block):
            target = m.group(4) or m.group(2)
            if target not in refs:
                refs.append(target)
        for m in _SOURCE_RE.finditer(block):
            name = f"{m.group(2)}.{m.group(4)}"
            if name not in sources:
                sources.append(name)
    return refs, sources


def _find_cycle(project_root: Path, model_name: str, refs: list[str]) -> list[str] | None:
    """Return a ref path model_name -> ... -> model_name if the new refs would form a cycle.

    dbt parse does not detect cycles (only graph compilation does), so check here.
    """
    graph: dict[str, list[str]] = {}
    for path, _layer in iter_model_files(project_root):
        if path.stem != model_name:
            try:
                graph[path.stem] = extract_refs(path.read_text())[0]
            except OSError:
                continue
    graph[model_name] = refs

    def walk(node: str, trail: list[str]) -> list[str] | None:
        """DFS for a path back to model_name."""
        for nxt in graph.get(node, []):
            if nxt == model_name:
                return [*trail, nxt]
            if nxt not in trail:
                found = walk(nxt, [*trail, nxt])
                if found:
                    return found
        return None

    return walk(model_name, [model_name])


def validate_model_sql(project_root: Path, model_name: str, layer: str, sql: str) -> list[str]:
    """Validate SQL and lineage; return warnings, raise ModelServiceError with all errors."""
    errors: list[str] = []
    warnings: list[str] = []
    if not sql.strip():
        raise ModelServiceError(["SQL is empty"])

    models = list_models(project_root)
    known = (
        set(models)
        | stems(project_root, "seeds", "*.csv")
        | stems(project_root, "snapshots", "*.sql")
    )
    refs, sources = extract_refs(sql)

    for target in refs:
        if target == model_name:
            errors.append(f"Model '{model_name}' references itself via ref('{target}')")
        elif target not in known:
            msg = f"ref('{target}') does not match any model, seed or snapshot"
            close = difflib.get_close_matches(target, sorted(known), n=1)
            if close:
                msg += f" (did you mean '{close[0]}'?)"
            errors.append(msg)
        elif layer == "intermediate" and models.get(target) == "marts":
            errors.append(f"intermediate models must not ref a marts model: ref('{target}')")
        elif layer == "staging" and models.get(target) in ("intermediate", "marts"):
            errors.append(f"staging models must not ref a {models[target]} model: ref('{target}')")

    if layer in CUSTOM_MODEL_LAYERS:
        for src_tbl in sources:
            src, tbl = src_tbl.split(".", 1)
            errors.append(
                f"{layer} models must not read raw sources directly; ref the staging model "
                f"(e.g. stg_{src}__{tbl}) instead of source('{src}', '{tbl}')"
            )
    if not errors:
        cycle = _find_cycle(project_root, model_name, [r for r in refs if r != model_name])
        if cycle:
            errors.append(f"ref cycle: {' -> '.join(cycle)}")
    if errors:
        raise ModelServiceError(errors)

    if layer != "staging":
        outside_jinja = _JINJA_BLOCK_RE.sub("", _JINJA_COMMENT_RE.sub("", sql))
        outside_jinja = re.sub(r"--[^\n]*", "", outside_jinja)
        if _RAW_TABLE_RE.search(outside_jinja):
            warnings.append(
                "SQL references a raw_ schema table directly; ref() the staging model instead"
            )
        if not refs and not sources:
            warnings.append("Model has no ref() or source() calls, so it has no upstream lineage")
    if layer == "marts" and not model_name.startswith(("fct_", "dim_")):
        warnings.append("Consider fct_ (facts) or dim_ (dimensions) for marts model names")
    return warnings


def _header_lines(model_name: str, layer: str, description: str, materialization: str) -> list[str]:
    timestamp = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"-- {model_name}",
        f"-- Created: {timestamp}",
    ]
    if description:
        lines.append(f"-- {description}")
    lines.append("")
    lines.append("{{ config(")
    lines.append(f"    materialized='{materialization}',")
    lines.append(f"    schema='{layer}'")
    lines.append(") }}")
    lines.append("")
    return lines


def render_model_sql(
    model_name: str,
    layer: str,
    *,
    sql: str | None = None,
    upstream: list[str] | None = None,
    description: str = "",
    materialization: str = "table",
) -> str:
    """Render the model file content (template when sql is None, else config-wrapped sql)."""
    if materialization != "table":
        raise ModelServiceError(
            [f"Unsupported materialization '{materialization}': all Dango models use 'table'"]
        )
    upstream_tables = upstream

    if sql is not None:
        body = sql.rstrip() + "\n"
        if layer == "staging" or _CONFIG_RE.search(sql):
            return body
        header = _header_lines(model_name, layer, description, materialization)
        return "\n".join(header) + "\n" + body.lstrip("\n")
    if layer == "staging":
        raise ModelServiceError(["sql is required for staging models"])

    lines = _header_lines(model_name, layer, description, materialization)

    if upstream_tables:
        lines.append("")
        # Derive aliases, handling collisions
        aliases: list[str] = []
        for table in upstream_tables:
            parts = table.split("_")
            alias = parts[-1] if len(parts) > 1 else table
            # Fall back to full table name if alias already used
            if alias in aliases:
                alias = table
            # If fallback also collides, add numeric suffix
            if alias in aliases:
                i = 2
                while f"{alias}_{i}" in aliases:
                    i += 1
                alias = f"{alias}_{i}"
            aliases.append(alias)

        # Generate CTE block with resolved aliases
        for i, (table, alias) in enumerate(zip(upstream_tables, aliases, strict=True)):
            comma = "," if i < len(upstream_tables) - 1 else ""
            lines.append(f"WITH {alias} AS (" if i == 0 else f"{alias} AS (")
            lines.append(f"    SELECT * FROM {{{{ ref('{table}') }}}}")
            lines.append(f"){comma}")
        lines.append("")
        lines.append("SELECT")
        lines.append("    -- TODO: Define your transformation here")
        lines.append(f"    {aliases[0]}.*")
        lines.append(f"FROM {aliases[0]}")
    else:
        lines.append("")
        lines.append("SELECT")
        lines.append("    -- TODO: Define your transformation here")
        lines.append("    1 AS placeholder")

    return "\n".join(lines) + "\n"
