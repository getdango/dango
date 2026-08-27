"""dango/catalog/manifest.py

Pure manifest-node readers — no I/O, no FastAPI dependency.
"""

from __future__ import annotations

import re
from typing import Any


def _build_test_status_map(
    manifest: dict[str, Any],
    run_results: dict[str, Any] | None,
) -> dict[str, list[dict[str, str | None]]]:
    """Map model/source unique_ids to their test results.

    Args:
        manifest: Parsed dbt manifest.
        run_results: Parsed ``run_results.json`` or ``None``.

    Returns:
        ``{model_unique_id: [{"name": ..., "status": "pass"|"fail"|"error"|None}]}``.
    """
    # Build status lookup from run_results
    result_status: dict[str, str] = {}
    if run_results:
        for r in run_results.get("results", []):
            uid = r.get("unique_id", "")
            raw_status = r.get("status", "")
            if uid and raw_status:
                # dbt uses "success"; normalize to "pass" for display
                result_status[uid] = "pass" if raw_status == "success" else raw_status

    test_map: dict[str, list[dict[str, str | None]]] = {}
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "test":
            continue
        test_name = node.get("name", uid)
        status = result_status.get(uid)
        for dep in node.get("depends_on", {}).get("nodes", []):
            test_map.setdefault(dep, []).append({"name": test_name, "status": status})

    return test_map


def _classify_model_type(node: dict[str, Any]) -> str:
    """Classify a dbt model as ``staging``, ``intermediate``, or ``marts``.

    Classification priority:
    1. Schema name: ``staging`` / ``intermediate`` / ``marts``.
    2. Name prefix: ``stg_`` → staging, ``fct_`` / ``dim_`` → marts,
       ``int_`` → intermediate.
    3. Fallback: ``intermediate``.

    Args:
        node: A manifest model node dict.

    Returns:
        One of ``"staging"``, ``"intermediate"``, ``"marts"``.
    """
    schema = (node.get("schema") or "").lower()
    if schema == "staging":
        return "staging"
    if schema == "intermediate":
        return "intermediate"
    if schema == "marts":
        return "marts"

    name = (node.get("name") or "").lower()
    if name.startswith("stg_"):
        return "staging"
    if name.startswith(("fct_", "dim_")):
        return "marts"
    if name.startswith("int_"):
        return "intermediate"

    return "intermediate"


def _model_profiling_key(node: dict[str, Any], kind: str) -> tuple[str, str] | None:
    """Return the ``(source, table_name)`` SQLite key used by the profiling write path.

    Mirrors :func:`dango.utils.post_sync.profile_table` callers so cached stats
    and row counts can be read back for any model type:
    - sources and staging models are keyed by the raw source name,
    - intermediate/marts models are keyed by their dbt schema,
    - seeds are keyed by their dbt schema (always "main" by default).

    Args:
        node: A manifest model, source, or seed node dict.
        kind: ``"model"``, ``"source"``, or ``"seed"``.

    Returns:
        ``(source, table_name)`` tuple or ``None`` if no key applies.
    """
    table = node.get("alias") or node.get("name", "")
    if not table:
        return None
    if kind == "seed":
        schema = node.get("schema", "main")
        return (schema, table) if table else None
    if kind == "source":
        source = node.get("source_name", "")
        return (source, table) if source else None
    schema = node.get("schema", "")
    if schema == "staging":
        m = re.match(r"^stg_(.+?)__", table)
        return (m.group(1), table) if m else None
    return (schema, table) if schema else None


def _find_model_in_manifest(
    manifest: dict[str, Any],
    model_name: str,
) -> tuple[str | None, dict[str, Any] | None, str]:
    """Find a model or source by name in the manifest.

    Models are preferred over sources when names collide.

    Args:
        manifest: Parsed dbt manifest.
        model_name: Name to search for.

    Returns:
        Tuple of ``(unique_id, node_dict, kind)`` where *kind* is
        ``"model"`` or ``"source"``.  Returns ``(None, None, "")``
        if not found.
    """
    # Search models first
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model" and node.get("name") == model_name:
            return uid, node, "model"
    # Then sources
    for uid, src in manifest.get("sources", {}).items():
        if src.get("name") == model_name:
            return uid, src, "source"
    return None, None, ""


def _search_manifest(
    manifest: dict[str, Any],
    query: str,
) -> list[dict[str, Any]]:
    """Search manifest models and sources by name, description, column names.

    Args:
        manifest: Parsed dbt manifest.
        query: Case-insensitive search string.

    Returns:
        List of search result dicts (max 50).
    """
    q = query.lower()
    name_matches: list[dict[str, Any]] = []
    desc_matches: list[dict[str, Any]] = []
    col_matches: list[dict[str, Any]] = []

    # Search models
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        name = node.get("name", "")
        desc = node.get("description", "")
        model_type = _classify_model_type(node)

        if q in name.lower():
            name_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": model_type,
                    "description": desc,
                    "match_type": "name",
                }
            )
            continue

        if desc and q in desc.lower():
            desc_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": model_type,
                    "description": desc,
                    "match_type": "description",
                }
            )
            continue

        for col_name in node.get("columns", {}):
            if q in col_name.lower():
                col_matches.append(
                    {
                        "unique_id": uid,
                        "name": name,
                        "type": model_type,
                        "description": desc,
                        "match_type": "column",
                        "matched_column": col_name,
                    }
                )
                break

    # Search sources
    for uid, src in manifest.get("sources", {}).items():
        name = src.get("name", "")
        desc = src.get("description", "")

        if q in name.lower():
            name_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": "source",
                    "description": desc,
                    "match_type": "name",
                }
            )
            continue

        if desc and q in desc.lower():
            desc_matches.append(
                {
                    "unique_id": uid,
                    "name": name,
                    "type": "source",
                    "description": desc,
                    "match_type": "description",
                }
            )
            continue

        for col_name in src.get("columns", {}):
            if q in col_name.lower():
                col_matches.append(
                    {
                        "unique_id": uid,
                        "name": name,
                        "type": "source",
                        "description": desc,
                        "match_type": "column",
                        "matched_column": col_name,
                    }
                )
                break

    results = name_matches + desc_matches + col_matches
    return results[:50]
