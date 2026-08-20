"""dango/catalog/lineage.py

dbt manifest DAG + impact analysis readers.
"""

from __future__ import annotations

from typing import Any

from dango.web.helpers import get_dbt_manifest


def _build_lineage_dag() -> dict[str, Any] | None:
    """Build full DAG from dbt manifest (sources + models + edges).

    Returns:
        Dict with ``nodes`` and ``edges`` lists, or ``None`` if no manifest.
    """
    manifest = get_dbt_manifest()
    if manifest is None:
        return None

    nodes_out: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    depended_on_by: dict[str, list[str]] = {}
    test_map: dict[str, list[str]] = {}

    # Collect model dependencies and test associations
    for uid, node in manifest.get("nodes", {}).items():
        rtype = node.get("resource_type", "")
        if rtype == "model":
            deps = node.get("depends_on", {}).get("nodes", [])
            for dep in deps:
                depended_on_by.setdefault(dep, []).append(uid)
                edges.append({"source": dep, "target": uid})
        elif rtype == "test":
            for dep in node.get("depends_on", {}).get("nodes", []):
                test_map.setdefault(dep, []).append(node.get("name", uid))

    # Build node list — models
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") != "model":
            continue
        columns = node.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        deps = node.get("depends_on", {}).get("nodes", [])
        tests = test_map.get(uid, [])
        nodes_out.append(
            {
                "id": uid,
                "name": node.get("name", ""),
                "type": "model",
                "schema": node.get("schema", ""),
                "materialization": node.get("config", {}).get("materialized"),
                "description": node.get("description", ""),
                "depends_on": deps,
                "depended_on_by": depended_on_by.get(uid, []),
                "test_count": len(tests),
                "test_names": tests,
                "has_description": bool(node.get("description")),
                "columns_documented": cols_documented,
                "columns_total": len(columns),
            }
        )

    # Build node list — sources
    for uid, src in manifest.get("sources", {}).items():
        columns = src.get("columns", {})
        cols_documented = sum(1 for c in columns.values() if c.get("description"))
        tests = test_map.get(uid, [])
        nodes_out.append(
            {
                "id": uid,
                "name": src.get("name", ""),
                "type": "source",
                "source_name": src.get("source_name", ""),
                "schema": src.get("schema", ""),
                "materialization": None,
                "description": src.get("description", ""),
                "depends_on": [],
                "depended_on_by": depended_on_by.get(uid, []),
                "test_count": len(tests),
                "test_names": tests,
                "has_description": bool(src.get("description")),
                "columns_documented": cols_documented,
                "columns_total": len(columns),
            }
        )

    return {"nodes": nodes_out, "edges": edges}


def _get_impact_tree(
    reverse_map: dict[str, list[str]],
    node_id: str,
    all_nodes: dict[str, dict[str, Any]],
    ancestors: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Recursively build downstream impact tree from a node.

    Uses an ancestor chain (not a shared visited set) so diamond dependencies
    expand fully under each branch while true back-edge cycles are detected.

    Args:
        reverse_map: Mapping of node_id → list of downstream node_ids.
        node_id: Starting node unique_id.
        all_nodes: Combined dict of all manifest nodes + sources.
        ancestors: Immutable set of ancestor node_ids for cycle detection.

    Returns:
        Tree dict with ``name``, ``type``, ``children``,
        ``total_downstream_count``, and optionally ``cycle``.
    """
    if ancestors is None:
        ancestors = frozenset()

    node = all_nodes.get(node_id, {})
    if node_id in ancestors:
        return {
            "name": node.get("name", node_id),
            "type": node.get("resource_type", "unknown"),
            "cycle": True,
            "children": [],
            "total_downstream_count": 0,
        }

    new_ancestors = ancestors | {node_id}
    children = []
    total = 0
    for child_id in reverse_map.get(node_id, []):
        child_tree = _get_impact_tree(reverse_map, child_id, all_nodes, new_ancestors)
        children.append(child_tree)
        total += 1 + child_tree["total_downstream_count"]

    return {
        "name": node.get("name", node_id),
        "type": node.get("resource_type", "unknown"),
        "children": children,
        "total_downstream_count": total,
    }


def _build_impact_response(
    manifest: dict[str, Any],
    model_name: str,
) -> dict[str, Any] | None:
    """Build impact analysis response from manifest (blocking helper).

    Searches models first, then sources, so models take priority when
    names collide (common in dbt: source "orders" + model "orders").

    Args:
        manifest: Parsed dbt manifest dict.
        model_name: Validated model/source name to look up.

    Returns:
        Impact response dict, or ``None`` if *model_name* not found.
    """
    all_nodes: dict[str, dict[str, Any]] = {}
    reverse_map: dict[str, list[str]] = {}

    for uid, node in manifest.get("nodes", {}).items():
        all_nodes[uid] = node
        if node.get("resource_type") == "model":
            for dep in node.get("depends_on", {}).get("nodes", []):
                reverse_map.setdefault(dep, []).append(uid)

    for uid, src in manifest.get("sources", {}).items():
        all_nodes[uid] = src

    # Find target — prefer models over sources for name collisions
    target_id: str | None = None
    for uid, node in manifest.get("nodes", {}).items():
        if node.get("resource_type") == "model" and node.get("name") == model_name:
            target_id = uid
            break
    if target_id is None:
        for uid in manifest.get("sources", {}):
            if all_nodes[uid].get("name") == model_name:
                target_id = uid
                break

    if target_id is None:
        return None

    tree = _get_impact_tree(reverse_map, target_id, all_nodes)
    direct = reverse_map.get(target_id, [])
    node_info = all_nodes[target_id]

    return {
        "model": model_name,
        "type": node_info.get("resource_type", "unknown"),
        "direct_dependents": [all_nodes.get(d, {}).get("name", d) for d in direct],
        "tree": tree,
        "total_downstream_count": tree["total_downstream_count"],
    }
