"""dango/cli/commands/mcp_debug.py

Read-only MCP debugging tools: project validation, logs, platform status, and warehouse health.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root
from dango.cli.commands.mcp_server import mcp

_SECRET_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|authorization|credential|private[_-]?key)"
)
_SECRET_INLINE_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|\S+)"
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_TAIL_BYTES = 2 * 1024 * 1024
_MAX_LINES = 500
_MAX_STR = 2000
_LOG_FILES = {
    "activity": Path(".dango") / "logs" / "activity.jsonl",
    "dango": Path(".dango") / "logs" / "dango.log",
    "dbt": Path("dbt") / "logs" / "dbt.log",
}


def _redact_obj(obj: Any) -> Any:
    """Recursively mask values whose KEY looks secret."""
    if isinstance(obj, dict):
        return {
            k: ("****" if isinstance(k, str) and _SECRET_KEY_RE.search(k) else _redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return _SECRET_INLINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", obj)
    return obj


def _truncate_obj(obj: Any) -> tuple[Any, bool]:
    """Truncate every string to _MAX_STR chars; return (obj, changed)."""
    if isinstance(obj, dict):
        out = {}
        changed = False
        for k, v in obj.items():
            out[k], c = _truncate_obj(v)
            changed = changed or c
        return out, changed
    if isinstance(obj, list):
        items = [_truncate_obj(v) for v in obj]
        return [i[0] for i in items], any(i[1] for i in items)
    if isinstance(obj, str) and len(obj) > _MAX_STR:
        return obj[:_MAX_STR] + "...", True
    return obj, False


def _read_tail_lines(path: Path) -> list[str]:
    """Last _TAIL_BYTES of a file as lines; drops a leading partial line."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        seeked = size > _TAIL_BYTES
        if seeked:
            f.seek(size - _TAIL_BYTES)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if seeked and lines:
        lines = lines[1:]
    return lines


def _lock_status(project_root: Path) -> dict[str, Any]:
    """Probe the dbt lock without ever creating, truncating or unlinking it."""
    state_dir = project_root / ".dango" / "state"
    lock_path = state_dir / "dbt.lock"
    info_path = state_dir / "dbt.lock.json"
    info: dict[str, Any] | None = None
    try:
        info = json.loads(info_path.read_text()) if info_path.exists() else None
    except (OSError, ValueError):
        info = None
    if sys.platform == "win32":
        return {"held": None, "holder": None, "last_holder": info}
    held = False
    if lock_path.exists():
        import fcntl

        try:
            with open(lock_path) as f:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    held = True
                else:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            held = False
    return {"held": held, "holder": info if held else None, "last_holder": None if held else info}


@mcp.tool()
def validate_project(
    check_connectivity: bool = False, include_passed: bool = False
) -> dict[str, Any]:
    """Run Dango's project validation (same checks as `dango validate`), read-only.

    Returns is_valid, pass/warn/fail counts, and the warn/fail checks (all checks when
    include_passed=True). check_connectivity=True makes live API calls to OAuth sources
    and can be slow.
    """
    try:
        from dango.cli.validate import ProjectValidator

        project_root = _get_project_root()
        summary = ProjectValidator(project_root).validate_all(
            display=False, include_connectivity=check_connectivity
        )
        checks = []
        for r in summary["results"]:
            if r.status == "pass" and not include_passed:
                continue
            category, check = r.name.split(": ", 1) if ": " in r.name else ("General", r.name)
            checks.append(
                {"category": category, "check": check, "status": r.status, "message": r.message}
            )
        return {
            "is_valid": summary["is_valid"],
            "counts": {"pass": summary["pass"], "warn": summary["warn"], "fail": summary["fail"]},
            "checks": _redact_obj(checks),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"Validation failed: {e}"}


@mcp.tool()
def get_logs(
    log: str = "activity",
    lines: int = 100,
    level: str | None = None,
    source: str | None = None,
    contains: str | None = None,
) -> dict[str, Any]:
    """Read the tail of a Dango log (secrets redacted, read-only).

    log: "activity" (.dango/logs/activity.jsonl), "dango" (.dango/logs/dango.log) or
    "dbt" (dbt/logs/dbt.log). lines is clamped to 1..500. Filters: level (equality),
    source (activity/dango only), contains (case-insensitive substring). Returns the
    last matching entries, oldest first.
    """
    try:
        if log not in _LOG_FILES:
            return {"error": f"Unknown log '{log}'. Valid values: {', '.join(_LOG_FILES)}"}
        project_root = _get_project_root()
        rel = _LOG_FILES[log]
        path = project_root / rel
        if not path.is_file():
            return {
                "log": log,
                "path": str(rel),
                "entries": [],
                "note": "Log file not found (nothing logged yet)",
            }
        lines = max(1, min(int(lines), _MAX_LINES))
        needle = contains.lower() if contains else None
        want_level = level.lower() if level else None
        entries: list[Any] = []
        for raw in _read_tail_lines(path):
            if not raw.strip():
                continue
            if log == "dbt":
                text = _ANSI_RE.sub("", raw)
                if needle and needle not in text.lower():
                    continue
                if want_level and not re.search(
                    rf"\[\s*{re.escape(want_level)}\s*\]", text, re.IGNORECASE
                ):
                    continue
                entries.append(
                    {"line": _SECRET_INLINE_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}****", text)}
                )
                continue
            if needle and needle not in raw.lower():
                continue
            try:
                parsed: Any = json.loads(raw)
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                if want_level or source:
                    continue
                entries.append({"raw": _redact_obj(raw)})
                continue
            if want_level and str(parsed.get("level", "")).lower() != want_level:
                continue
            if source:
                val = parsed.get("source", parsed.get("sources"))
                if not (val == source or (isinstance(val, list) and source in val)):
                    continue
            entries.append(_redact_obj(parsed))
        entries = entries[-lines:]
        entries, truncated = _truncate_obj(entries)
        return {"log": log, "path": str(rel), "entries": entries, "truncated": truncated}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not read logs: {e}"}


@mcp.tool()
def get_platform_status() -> dict[str, Any]:
    """Show whether this project's Dango server is running, the dbt/sync write-lock
    holder, and warehouse file info (read-only)."""
    try:
        import dango
        from dango.cli.helpers.process_manager import is_project_server_running
        from dango.config.helpers import load_config

        project_root = _get_project_root()
        config = load_config(project_root)
        running = is_project_server_running(project_root)
        port = config.platform.port
        wh = project_root / "data" / "warehouse.duckdb"
        exists = wh.exists()
        return {
            "project": config.project.name,
            "dango_version": dango.__version__,
            "server_running": running,
            "port": port,
            "web_url": f"http://localhost:{port}" if running else None,
            "dbt_lock": _lock_status(project_root),
            "warehouse": {
                "path": "data/warehouse.duckdb",
                "exists": exists,
                "size_mb": round(wh.stat().st_size / 1_048_576, 1) if exists else None,
            },
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not get platform status: {e}"}


_BUSY = "Warehouse is busy (a sync or dbt build is writing); retry shortly"


def _warehouse_error(e: Exception) -> dict[str, Any]:
    text = str(e)
    if "lock" in text.lower() or "already open" in text.lower():
        return {"error": _BUSY}
    return {"error": text}


@mcp.tool()
def get_warehouse_health() -> dict[str, Any]:
    """Find orphaned raw tables (no matching configured source) and report DuckDB health
    (read-only)."""
    try:
        from dango.config.helpers import load_config
        from dango.utils.db_health import check_duckdb_health, find_orphaned_tables

        project_root = _get_project_root()
        db_path = project_root / "data" / "warehouse.duckdb"
        if not db_path.exists():
            return {"error": "No warehouse found. Run a sync first."}
        try:
            orphans = find_orphaned_tables(db_path, load_config(project_root))
            health = check_duckdb_health(db_path)
        except Exception as e:  # noqa: BLE001
            return _warehouse_error(e)
        return {
            "orphaned_tables": [{"schema": s, "table": t, "rows": r} for s, t, r in orphans],
            "health": json.loads(json.dumps(health, default=str)),
        }
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not check warehouse health: {e}"}
