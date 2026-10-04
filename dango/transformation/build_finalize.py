"""dango/transformation/build_finalize.py

Shared post-`dbt build` steps (model status, schema.yml sync, Metabase refresh) and run_results summaries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dango.logging import get_logger

logger = get_logger(__name__)

_MESSAGE_CAP = 2000
_FAILED_STATUSES = frozenset({"error", "fail", "runtime error"})


def _parse_unique_id(unique_id: str) -> tuple[str, str]:
    """Return (resource_type, name) from a dbt unique_id.

    ``model.proj.name`` -> name; ``test.proj.name.<hash>`` -> name (hash dropped).
    """
    parts = unique_id.split(".")
    resource_type = parts[0] if parts else ""
    if len(parts) < 3:
        return resource_type, unique_id
    if resource_type == "test" and len(parts) >= 4:
        return resource_type, ".".join(parts[2:-1])
    return resource_type, ".".join(parts[2:])


def summarize_run_results(
    project_root: Path, *, since: float | None = None
) -> dict[str, Any] | None:
    """Summarise dbt/target/run_results.json, or None if missing/unreadable.

    If ``since`` (epoch seconds) is given and the file is older than it, it belongs to a
    previous build (dbt writes nothing on compile/parse errors) and None is returned.

    Returns {"elapsed_seconds": float | None,
             "counts": {"success": n, "error": n, "fail": n, "warn": n, "skipped": n, ...},
             "nodes": [{"unique_id", "name", "resource_type", "status", "message",
                        "execution_time", "failures"}],
             "failed": [<the nodes whose status is error/fail/runtime error>]}
    """
    try:
        path = project_root / "dbt" / "target" / "run_results.json"
        if not path.exists():
            return None
        if since is not None and path.stat().st_mtime < since:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        nodes: list[dict[str, Any]] = []
        for res in data.get("results", []):
            unique_id = str(res.get("unique_id") or "")
            if not unique_id:
                continue
            resource_type, name = _parse_unique_id(unique_id)
            status = str(res.get("status"))
            message = res.get("message")
            if message is not None:
                message = str(message)[:_MESSAGE_CAP]
            counts[status] = counts.get(status, 0) + 1
            nodes.append(
                {
                    "unique_id": unique_id,
                    "name": name,
                    "resource_type": resource_type,
                    "status": status,
                    "message": message,
                    "execution_time": res.get("execution_time"),
                    "failures": res.get("failures"),
                }
            )
        for key in ("success", "error", "fail", "warn", "skipped"):
            counts.setdefault(key, 0)
        elapsed = data.get("elapsed_time")
        return {
            "elapsed_seconds": float(elapsed) if isinstance(elapsed, (int, float)) else None,
            "counts": counts,
            "nodes": nodes,
            "failed": [n for n in nodes if n["status"].lower() in _FAILED_STATUSES],
        }
    except Exception:  # noqa: BLE001 - summary is best-effort, never raises
        logger.warning("run_results_summary_failed", exc_info=True)
        return None


def _collect_model_names(project_root: Path) -> list[str]:
    models: list[str] = []
    for layer in ["intermediate", "marts"]:
        layer_dir = project_root / "dbt" / "models" / layer
        if layer_dir.exists():
            for sql_file in layer_dir.rglob("*.sql"):
                if not sql_file.name.startswith("_"):
                    models.append(sql_file.stem)
    return models


def finalize_dbt_build(
    project_root: Path, *, success: bool, refresh_metabase: bool = True
) -> dict[str, Any]:
    """Run after `dbt build` (lock already released). Never raises.

    Always: update_model_status(project_root).
    On success: update_model_schemas(project_root, <every intermediate/marts model stem, rglob,
    skipping names starting with "_">) and, if refresh_metabase, refresh_metabase_connection +
    sync_metabase_schema (same calls/order as transform.py).
    Returns {"model_status": "updated"|"error", "schema_sync": "updated"|"skipped"|"error",
             "metabase": "refreshed"|"not_running"|"skipped",
             "metabase_schema_synced": bool}  # only meaningful when metabase == "refreshed"
    """
    out: dict[str, Any] = {
        "model_status": "updated",
        "schema_sync": "skipped",
        "metabase": "skipped",
        "metabase_schema_synced": False,
    }

    try:
        from dango.utils.dbt_status import update_model_status

        update_model_status(project_root)
    except Exception:  # noqa: BLE001
        logger.warning("model_status_update_failed", exc_info=True)
        out["model_status"] = "error"

    if not success:
        return out

    try:
        from dango.cli.schema_manager import update_model_schemas

        models = _collect_model_names(project_root)
        if models:
            update_model_schemas(project_root, models)
            out["schema_sync"] = "updated"
    except Exception:  # noqa: BLE001
        logger.warning("schema_sync_failed", exc_info=True)
        out["schema_sync"] = "error"

    if refresh_metabase:
        try:
            from dango.visualization.metabase import (
                refresh_metabase_connection,
                sync_metabase_schema,
            )

            mb_ok, _mb_err, mb_session_id = refresh_metabase_connection(project_root)
            if mb_ok:
                out["metabase"] = "refreshed"
                out["metabase_schema_synced"] = bool(
                    sync_metabase_schema(project_root, existing_session_id=mb_session_id)
                )
            else:
                out["metabase"] = "not_running"
        except Exception:  # noqa: BLE001
            logger.warning("metabase_refresh_failed", exc_info=True)
            out["metabase"] = "skipped"

    return out
