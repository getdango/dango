"""dango/cli/commands/mcp_operations.py

MCP operate tools: sync sources, run dbt transforms, and check credential health.
"""

from __future__ import annotations

from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root
from dango.cli.commands.mcp_server import mcp

# All other dango.* imports are lazy (inside function bodies), and nothing here
# may write to stdout: the server speaks JSON-RPC over stdio (see mcp_server.py).


@mcp.tool()
def run_sync(
    source_name: str | None = None,
    full_refresh: bool = False,
    since: str | None = None,
    until: str | None = None,
    backfill: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    allow_schema_changes: bool = False,
    allow_empty_replace: bool | None = None,
) -> dict[str, Any]:
    """Sync data sources. Equivalent to `dango sync`.

    Blocks until the sync finishes. First syncs of large sources can take a long
    time; clients may need a longer tool timeout (e.g. Claude Code's MCP_TOOL_TIMEOUT).

    Args:
        source_name: Source to sync (as defined in sources.yml). Omit to sync all
            enabled sources. A named disabled source is skipped by the sync (a warning says so).
        full_refresh: If True, drop existing data and reload everything (default False).
        since: Override start date, YYYY-MM-DD. Conflicts with backfill.
        until: Override end date, YYYY-MM-DD. Conflicts with backfill.
        backfill: Backfill the last N days, e.g. '7d', '2w', '1m' (d=days, w=weeks,
            m=30 days). Conflicts with since/until.
        limit: Dev mode: maximum rows per source (positive integer). Ignored by CSV sources.
        dry_run: If True, only report what would be synced; nothing is loaded.
        allow_schema_changes: Allow CSV schema evolution during this run.
        allow_empty_replace: True overrides the source's saved empty-sync policy to
            allow a zero-row replace-mode sync; False forces blocking; omit to use
            the saved policy.

    Returns dict with: status (completed/partial/failed/dry_run), the sync summary
    keys (success_count, failed_count, ...), warnings, metabase
    (refreshed/not_running), or error.
    """
    from datetime import datetime, timedelta

    project_root = _get_project_root()

    if backfill and (since or until):
        return {"error": "backfill conflicts with since/until. Use one or the other."}
    if limit is not None and limit <= 0:
        return {"error": "limit must be a positive integer."}

    start: datetime | None = None
    end: datetime | None = None
    if backfill:
        import click

        from dango.cli.commands.source import _parse_duration

        try:
            days = _parse_duration(backfill)
        except click.BadParameter as e:
            return {"error": e.format_message()}
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        start = today - timedelta(days=days)
        end = today
    if since:
        try:
            start = datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            return {"error": "Invalid since date format. Use YYYY-MM-DD"}
    if until:
        try:
            end = datetime.strptime(until, "%Y-%m-%d")
        except ValueError:
            return {"error": "Invalid until date format. Use YYYY-MM-DD"}
    if start and end and start >= end:
        return {"error": "since must be before until."}

    from dango.config.helpers import check_unreferenced_custom_sources, load_config

    try:
        config = load_config(project_root)
    except Exception as e:
        return {"error": f"Could not load project configuration: {e}"}

    if source_name:
        source = config.sources.get_source(source_name)
        if source is None:
            return {
                "error": f"Source '{source_name}' not found in sources.yml",
                "available": [
                    {"name": s.name, "type": s.type.value, "enabled": s.enabled}
                    for s in config.sources.sources
                ],
            }
        sources_to_sync = [source]
    else:
        sources_to_sync = config.sources.get_enabled_sources()
        if not sources_to_sync:
            return {"error": "No enabled sources found in sources.yml"}

    from dango.cli.commands.source import _source_supports_date_range

    warnings: list[str] = []
    unreferenced = check_unreferenced_custom_sources(project_root, config.sources)
    if unreferenced:
        warnings.append(f"Unreferenced custom sources: {', '.join(unreferenced)}")
    has_date_range = start is not None or end is not None
    for src in sources_to_sync:
        src_type = src.type.value
        if has_date_range and not _source_supports_date_range(src_type):
            warnings.append(
                f"'{src.name}' ({src_type}) does not support date range filtering "
                "— dates will be ignored"
            )
        if not src.enabled:
            warnings.append(
                f"'{src.name}' is disabled — the sync skips disabled sources, "
                "so nothing will be loaded. Enable it in sources.yml first."
            )
        if limit and src_type == "csv":
            warnings.append(f"'{src.name}' (csv) does not support limit — CSV loads all rows")
    if full_refresh:
        warnings.append("Full refresh: existing data will be dropped and reloaded")

    if dry_run:
        return {
            "status": "dry_run",
            "sources": [
                {"name": s.name, "type": s.type.value, "enabled": s.enabled}
                for s in sources_to_sync
            ],
            "options": {
                "full_refresh": full_refresh,
                "since": start.strftime("%Y-%m-%d") if start else None,
                "until": end.strftime("%Y-%m-%d") if end else None,
                "limit": limit,
                "allow_schema_changes": allow_schema_changes,
                "allow_empty_replace": allow_empty_replace,
            },
            "warnings": warnings,
        }

    from dango.exceptions import OAuthTokenExpiredError, OAuthTokenRevokedError
    from dango.oauth.validation import validate_before_sync

    for src in sources_to_sync:
        try:
            validate_before_sync(src.type.value, project_root)
        except (OAuthTokenRevokedError, OAuthTokenExpiredError) as e:
            return {
                "status": "failed",
                "error": e.user_message,
                "source": src.name,
                "action": f"dango oauth {src.type.value}",
            }
        except Exception as e:
            return {"status": "failed", "error": str(e), "source": src.name}

    from dango.ingestion import run_sync as _run_sync

    try:
        summary = _run_sync(
            project_root=project_root,
            sources=sources_to_sync,
            start_date=start,
            end_date=end,
            full_refresh=full_refresh,
            limit=limit,
            allow_schema_changes=allow_schema_changes,
            allow_empty_replace=allow_empty_replace,
        )
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    if not isinstance(summary, dict):
        return {"status": "completed"}

    failed_count = summary.get("failed_count", 0)
    if failed_count == 0:
        # Best-effort Metabase schema refresh, same as `dango sync`.
        try:
            from dango.visualization.metabase import (
                refresh_metabase_connection,
                sync_metabase_schema,
            )

            mb_ok, _mb_err, mb_session_id = refresh_metabase_connection(project_root)
            refreshed = mb_ok and sync_metabase_schema(
                project_root, existing_session_id=mb_session_id
            )
            summary["metabase"] = "refreshed" if refreshed else "not_running"
        except Exception:
            summary["metabase"] = "not_running"

    if failed_count == 0:
        summary["status"] = "completed"
    elif summary.get("success_count", 0) == 0:
        summary["status"] = "failed"
    else:
        summary["status"] = "partial"
    summary["warnings"] = warnings
    return summary


@mcp.tool()
def run_transform(select: str | None = None, full_refresh: bool = False) -> dict[str, Any]:
    """Run dbt transformations. Equivalent to `dango run`.

    Runs `dbt build`, so models AND tests run. Afterwards it records persistent model
    status, syncs schema.yml columns for intermediate/marts models, and refreshes
    Metabase (the last two only when the build had no failures).

    Unlike the dbt step inside `run_sync` (which tolerates test-only failures), this
    reports any failing model or test as status "failed", like `dango run`.

    Args:
        select: dbt --select expression (e.g. "stg_stripe+", "marts"). Runs all if omitted.
        full_refresh: If True, rebuild incremental models from scratch.

    Returns dict with:
        status: "completed" or "failed".
        output: dbt output, ANSI codes stripped, last 20 000 characters.
        results: summary of this build's run_results.json (null if dbt wrote none, e.g. a
            compile/parse error; `results_note` then explains): elapsed_seconds, counts
            (per status), nodes (every model/test/seed: name, resource_type, status,
            message, execution_time, failures) and failed (the nodes that errored or
            failed, with dbt's message saying why, e.g. which test failed).
        post_build: model_status, schema_sync, metabase outcomes of the follow-up steps.
        error: only when the call itself failed (e.g. dbt lock timeout).
    """
    project_root = _get_project_root()
    import contextlib
    import re
    import sys
    import time

    from dango.platform.common.metabase_lifecycle import (
        start_metabase_after_writes,
        stop_metabase_for_writes,
    )
    from dango.transformation import run_dbt_models
    from dango.transformation.build_finalize import finalize_dbt_build, summarize_run_results
    from dango.utils import DbtLock

    # Single-writer DuckDB (VAL-003): hold DbtLock for the dbt write only, like
    # transform.py's run(). A lock timeout (DbtLockError) falls through to the
    # except below.
    lock = DbtLock(
        project_root=project_root,
        source="mcp",
        operation=f"run_transform select={select}" if select else "run_transform",
    )
    try:
        lock.acquire()
        # Stop Metabase on cloud to prevent DuckDB lock conflicts during dbt writes.
        metabase_was_stopped = stop_metabase_for_writes(project_root)
        try:
            # run_dbt_models() returns tuple[bool, str] (success, output) and treats
            # test-only failures as success (sync semantics) — corrected below.
            build_started = time.time()
            ok, output = run_dbt_models(project_root, select=select, full_refresh=full_refresh)
        finally:
            if metabase_was_stopped:
                start_metabase_after_writes(project_root)
        try:
            lock.release()
        except Exception:  # noqa: BLE001 - build finished; the finally retries
            pass

        results = summarize_run_results(project_root, since=build_started)
        success = bool(ok and not (results and results["failed"]))
        # run_dbt_models already updated model status on success; finalize repeats it
        # (idempotent) and also covers the failure path.
        # schema_manager/metabase helpers print via Rich to stdout, which is the
        # JSON-RPC channel here — reroute to stderr.
        with contextlib.redirect_stdout(sys.stderr):
            post = finalize_dbt_build(project_root, success=success)
        clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", output)[-20000:]
        response: dict[str, Any] = {
            "status": "completed" if success else "failed",
            "output": clean,
            "results": results,
            "post_build": post,
        }
        if results is None and not success:
            response["results_note"] = (
                "dbt produced no run results for this build (likely a compile/parse error)"
                " — see output"
            )
        return response
    except Exception as e:
        return {"status": "failed", "error": str(e)}
    finally:
        if lock._acquired:
            lock.release()


@mcp.tool()
def run_doctor() -> list[dict[str, Any]]:
    """Check credential health for all configured sources. Equivalent to `dango doctor`.

    Always fresh: bypasses the 5-minute credential-health cache, since this MCP server is
    long-lived and the user may have just set a key.

    Returns list of dicts with: source, type, status (ok/missing/expired), detail.
    """
    project_root = _get_project_root()
    # Correction (coordinating-chat pre-dispatch verification, 2026-09-03):
    # run_doctor_cached does not exist anywhere in the codebase (the CLI's
    # own `dango doctor` command, cli/commands/doctor.py, calls this
    # function directly — verified by reading it). Already returns the
    # exact list[dict[str, Any]] shape this tool's docstring promises.
    from dango.ingestion.credential_health import get_cached_credential_health

    return get_cached_credential_health(project_root, refresh=True)
