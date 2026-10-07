"""dango/cli/commands/mcp_remote.py

MCP remote tools: status, logs, history, query, sync and push for a deployed Dango server.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shlex
from pathlib import Path
from typing import Any

from dango.cli.commands.mcp_helpers import _get_project_root
from dango.cli.commands.mcp_server import mcp

_NO_DEPLOYMENT = {"error": "No cloud deployment configured — run `dango deploy` (CLI) first"}
_MAX_QUERY_SQL_LENGTH = 102_400
_MAX_LOG_LINES = 500
_MAX_LOG_BYTES = 200_000
_MAX_FILES = 200
_MAX_ERR = 2000

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PII_NAMES_CODE = (
    "import json; from pathlib import Path; "
    "from dango.governance.pii_masking import get_pii_column_names as g; "
    "print(json.dumps(sorted(g(Path('{root}')))))"
)


def _cloud(project_root: Path) -> Any:
    """CloudConfig with a droplet_ip, or None when no deployment is configured."""
    from dango.config.loader import ConfigLoader

    cfg = ConfigLoader(project_root).load_cloud_config()
    if cfg is None or cfg.droplet_ip is None:
        return None
    return cfg


def _ssh(cloud_cfg: Any, project_root: Path) -> Any:
    """Connected SSHManager (mirrors remote_mgmt._make_ssh_manager; pinned known_hosts)."""
    from dango.cli.commands.remote_mgmt import _make_ssh_manager

    ssh = _make_ssh_manager(cloud_cfg, project_root)
    ssh.connect(cloud_cfg.droplet_ip)
    return ssh


def _safe(text: str) -> str:
    from dango.cli.commands.mcp_debug import _redact_text

    text = _redact_text(_ANSI_RE.sub("", text or ""))
    return text if len(text) <= _MAX_ERR else text[:_MAX_ERR] + "..."


def _redact_obj(obj: Any) -> Any:
    from dango.cli.commands.mcp_debug import _redact_obj as redact

    return redact(obj)


def _json_safe(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=str))


def _run(fn: Any) -> dict[str, Any] | list[Any]:
    """Resolve project + cloud config + SSH, call fn(ssh, cloud_cfg, project_root); never raise."""
    try:
        project_root = _get_project_root()
        cloud_cfg = _cloud(project_root)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not load cloud configuration: {_safe(str(e))}"}
    if cloud_cfg is None:
        return dict(_NO_DEPLOYMENT)
    try:
        ssh = _ssh(cloud_cfg, project_root)
    except Exception as e:  # noqa: BLE001
        return {"error": f"Cannot connect to server: {_safe(str(e))}"}
    try:
        return fn(ssh, cloud_cfg, project_root)  # type: ignore[no-any-return]
    except Exception as e:  # noqa: BLE001
        return {"error": f"Remote operation failed: {_safe(str(e))}"}
    finally:
        try:
            ssh.disconnect()
        except Exception:  # noqa: BLE001
            pass


@mcp.tool()
def remote_status() -> dict[str, Any]:
    """Show the deployed server's health: resources, services, DuckDB size, versions,
    last syncs, last deployment and backup (read-only, over SSH)."""

    def go(ssh: Any, cfg: Any, _root: Path) -> dict[str, Any]:
        """Operation body run over the connected SSH session."""
        from dango.platform.cloud.server_status import collect_server_status

        status = _json_safe(dataclasses.asdict(collect_server_status(ssh, cfg)))
        return _redact_obj(status)  # type: ignore[no-any-return]

    return _run(go)  # type: ignore[return-value]


@mcp.tool()
def remote_logs(service: str = "dango", lines: int = 100) -> dict[str, Any]:
    """Read the tail of a remote service log (no follow; secrets redacted, read-only).

    service: "dango", "caddy" or "metabase". lines is clamped to 1..500; output is capped
    at 200 KB (oldest lines dropped). Only secret patterns are redacted: log text can
    still contain non-secret personal data.
    """
    from dango.platform.cloud.backup import PROJECT_DIR
    from dango.platform.cloud.service_logs import (
        LOG_SERVICES,
        NO_CONTAINER_MSG,
        build_log_command,
    )

    if service not in LOG_SERVICES:
        return {"error": f"Unknown service '{service}'. Valid values: {', '.join(LOG_SERVICES)}"}
    try:
        n = max(1, min(int(lines), _MAX_LOG_LINES))
    except (TypeError, ValueError):
        return {"error": "lines must be an integer"}

    def go(ssh: Any, _cfg: Any, _root: Path) -> dict[str, Any]:
        """Operation body run over the connected SSH session."""
        cmd = build_log_command(ssh, service, n, PROJECT_DIR)
        if cmd is None:
            return {"error": NO_CONTAINER_MSG.format(service=service)}
        res = ssh.exec_command(cmd, check=False)
        if not res.success:
            return {"error": f"Could not read {service} logs: {_safe(res.stderr or 'no output')}"}
        text = (res.stdout or "") + (res.stderr or "")  # docker logs writes to stderr
        out = [_safe_line(ln) for ln in text.split("\n") if ln.strip()][-n:]
        truncated = False
        while len(out) > 1 and sum(len(x) + 1 for x in out) > _MAX_LOG_BYTES:
            out = out[1:]
            truncated = True
        return {"service": service, "lines": out, "truncated": truncated}

    return _run(go)  # type: ignore[return-value]


def _safe_line(line: str) -> str:
    from dango.cli.commands.mcp_debug import _redact_text

    line = _redact_text(_ANSI_RE.sub("", line.rstrip("\r")))
    return line if len(line) <= _MAX_ERR else line[:_MAX_ERR] + "..."


@mcp.tool()
def remote_history(limit: int = 10) -> Any:
    """Recent deployment history from the server's deploy journal, newest first
    (limit clamped to 1..100)."""

    def go(ssh: Any, _cfg: Any, _root: Path) -> Any:
        """Operation body run over the connected SSH session."""
        from dango.platform.cloud.deploy_journal import read_remote_journal

        entries = read_remote_journal(ssh, limit=max(1, min(int(limit), 100)), raise_on_error=True)
        return _redact_obj(_json_safe(entries))

    return _run(go)


def _dango_cmd(inner: str) -> str:
    """Run `inner` as the dango user from the project dir (as remote_sync does), so SQLite
    WAL files are never created root-owned."""
    from dango.cli.commands.remote_sync import _PROJECT_ROOT

    return f"cd {_PROJECT_ROOT} && sudo -u dango -H {inner}"


def _remote_pii_names(ssh: Any) -> set[str] | None:
    from dango.cli.commands.remote_mgmt import _VENV_PYTHON
    from dango.cli.commands.remote_sync import _PROJECT_ROOT

    code = _PII_NAMES_CODE.format(root=_PROJECT_ROOT)
    try:
        res = ssh.exec_command(
            _dango_cmd(f"{_VENV_PYTHON} -c {shlex.quote(code)}"), timeout=30, check=False
        )
        if not res.success:
            return None
        names = json.loads(res.stdout.strip())
        return {str(n).lower() for n in names} if isinstance(names, list) else None
    except Exception:  # noqa: BLE001
        return None


def _query_error(stderr: str) -> str:
    text = (stderr or "").strip()
    if not text:
        return "Query failed with no error output."
    try:
        err = json.loads(text)
        if isinstance(err, dict):
            for key in ("message", "error", "detail"):
                if err.get(key):
                    return _safe(str(err[key]))
    except ValueError:
        pass
    return _safe(text)


@mcp.tool()
def remote_query(sql: str, timeout: int = 30) -> dict[str, Any]:
    """Run a read-only SELECT against the deployed server's warehouse via its /api/query
    endpoint (server-side auth, validation and audit logging apply).

    sql: single SELECT / WITH ... SELECT, max 102,400 characters. timeout: 1..120 seconds.
    Returns columns, rows, row_count, truncated. PII-flagged columns (local findings plus
    the server's) are masked by output name only, as in local `query` (aliases and
    expressions are not covered); opt out locally with api.mcp_mask_pii: false.
    """
    from dango.cli.commands.mcp_helpers import _validate_select_only

    if len(sql) > _MAX_QUERY_SQL_LENGTH:
        return {"error": f"SQL query too long (max {_MAX_QUERY_SQL_LENGTH} characters)"}
    try:
        _validate_select_only(sql)
    except ValueError as e:
        return {"error": str(e)}
    try:
        secs = max(1, min(int(timeout), 120))
    except (TypeError, ValueError):
        return {"error": "timeout must be an integer"}

    def go(ssh: Any, _cfg: Any, project_root: Path) -> dict[str, Any]:
        """Operation body run over the connected SSH session."""
        from dango.cli.commands.mcp_governance import _mcp_pii_mask_columns
        from dango.cli.commands.remote_mgmt import _AUTH_DB, _QUERY_SCRIPT, _VENV_PYTHON
        from dango.governance.pii_masking import mask_query_result

        script = shlex.quote(_QUERY_SCRIPT.format(auth_db=_AUTH_DB))
        cmd = _dango_cmd(f"{_VENV_PYTHON} -c {script} {shlex.quote(sql)} {secs}")
        res = ssh.exec_command(cmd, timeout=secs + 10, check=False)
        if not res.success:
            return {"error": _query_error(res.stderr)}
        try:
            data = json.loads((res.stdout or "").strip())
        except ValueError:
            return {"error": "Server returned an unreadable query response"}
        if not isinstance(data, dict):
            return {"error": "Server returned an unreadable query response"}
        if "error" in data:
            return {"error": _safe(str(data["error"]))}
        result = {
            "columns": data.get("columns", []),
            "rows": data.get("rows", []),
            "row_count": data.get("row_count", len(data.get("rows", []))),
            "truncated": bool(data.get("truncated", False)),
        }
        if data.get("warning"):
            result["warning"] = data["warning"]
        local = _mcp_pii_mask_columns(project_root)
        if local is None:
            return result
        remote = _remote_pii_names(ssh)
        masked = mask_query_result(result, local | (remote or set()))
        if remote is None:
            masked["pii_masking"]["note"] = (
                "Server PII findings unavailable (older server?); masked with local findings "
                "only. " + masked["pii_masking"]["note"]
            )
        return masked

    return _run(go)  # type: ignore[return-value]


@mcp.tool()
def remote_sync(
    source_name: str,
    full_refresh: bool = False,
    backfill: str | None = None,
    wait: bool = True,
) -> dict[str, Any]:
    """Trigger a sync of one source on the deployed server.

    The server runs the config it was last pushed: sources or changes not yet pushed
    (remote_push) are not on the server. source_name must exist in the local sources.yml.
    backfill: e.g. "7d", "2w", "1m". wait=True blocks until the sync finishes (up to 1 hour,
    which can exceed the MCP client's own timeout; the sync keeps running on the server);
    wait=False starts it in the background and returns immediately.
    """
    try:
        from dango.cli.commands.mcp_governance import _check_source

        project_root = _get_project_root()
        if _cloud(project_root) is None:
            return dict(_NO_DEPLOYMENT)
        err = _check_source(project_root, source_name)
        if err:
            return err
        backfill_days: int | None = None
        if backfill is not None:
            from dango.validation import parse_backfill_duration

            try:
                backfill_days = parse_backfill_duration(backfill)
            except ValueError as e:
                return {"error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not validate source: {_safe(str(e))}"}

    def go(ssh: Any, _cfg: Any, _root: Path) -> dict[str, Any]:
        """Operation body run over the connected SSH session."""
        from dango.cli.commands.remote_mgmt import _VENV_PYTHON
        from dango.cli.commands.remote_sync import _PROJECT_ROOT

        payload: dict[str, object] = {
            "sources": [source_name],
            "full_refresh": full_refresh,
            "project_root": _PROJECT_ROOT,
        }
        if backfill_days is not None:
            payload["backfill_days"] = backfill_days
        sudo_cmd = (
            f"sudo -u dango -H env DANGO_CLOUD_MODE=true"
            f" {_VENV_PYTHON} -m dango.platform.scheduling.sync_trigger"
            f" {shlex.quote(json.dumps(payload))}"
        )
        cmd = f"cd {_PROJECT_ROOT} && {sudo_cmd}"
        if not wait:
            from dango.platform.cloud.remote_launch import build_background_launch

            script = build_background_launch(_PROJECT_ROOT, sudo_cmd)
            res = ssh.exec_command(f"sh -c {shlex.quote(script)}", timeout=30, check=False)
            if not res.success:
                return {"status": "failed", "error": _safe(res.stderr or "Could not start sync")}
            return {"status": "started", "source": source_name}
        res = ssh.exec_command(cmd, timeout=3600, check=False)
        if res.success and res.stdout:
            try:
                data = json.loads(res.stdout.strip())
            except ValueError:
                return {"status": "unknown", "output": _safe(res.stdout)}
            return _json_safe(data) if isinstance(data, dict) else {"status": "unknown"}
        return {"status": "failed", "error": _safe(res.stderr or "No output")}

    return _run(go)  # type: ignore[return-value]


def _deploy_result(result: Any, warnings: list[str]) -> dict[str, Any]:
    sr = result.sync_result
    out: dict[str, Any] = {
        "dry_run": bool(result.dry_run),
        "files_synced_count": len(sr.synced_files),
        "files_synced": list(sr.synced_files[:_MAX_FILES]),
        "added_models": list(sr.added_models),
        "changed_models": list(sr.changed_models),
        "removed_models": list(sr.removed_models),
        "packages_changed": sr.packages_changed,
        "is_first_deploy": sr.is_first_deploy,
        "dbt_deps_run": result.dbt_deps_run,
        "dbt_compile_success": result.dbt_compile_success,
        "models_rebuilt": list(result.models_rebuilt),
        "duration_seconds": result.duration_seconds,
        "warnings": [_safe(w) for w in [*warnings, *result.warnings]],
    }
    if len(sr.synced_files) > _MAX_FILES:
        out["files_synced_truncated"] = True
    if result.backup_result is not None:
        out["backup"] = {
            "archive_path": result.backup_result.archive_path,
            "duration_seconds": result.backup_result.duration_seconds,
        }
    gi = result.git_info
    if gi is not None:
        out["git"] = {"commit": gi.commit_sha, "branch": gi.branch}
    return out


@mcp.tool()
def remote_push(dry_run: bool = True, confirm: bool = False) -> dict[str, Any]:
    """Push local config and dbt files to the deployed server and rebuild changed models.

    Defaults to a dry run that only lists what would change. A real push needs
    dry_run=False AND confirm=True; call it only after showing the user the dry-run result
    and getting explicit approval. Git guardrails (deploy branch, clean tree) are enforced
    with no overrides, and a held deploy lock is never forced. Rollback is CLI-only.
    """
    if not dry_run and not confirm:
        return {
            "error": (
                "A real push deploys local files to the production server and rebuilds "
                "models. Run remote_push(dry_run=True) first, show the user the changes, and "
                "call remote_push(dry_run=False, confirm=True) only after the user "
                "explicitly approves."
            )
        }
    try:
        project_root = _get_project_root()
        cloud_cfg = _cloud(project_root)
        if cloud_cfg is None:
            return dict(_NO_DEPLOYMENT)
        from dango.utils.git_info import check_git_guardrails, collect_git_info

        git_info = collect_git_info(project_root)
        warnings: list[str] = []
        if git_info.is_git_repo:
            guardrails = check_git_guardrails(
                git_info,
                expected_branch=cloud_cfg.deploy_branch,
                allow_dirty=False,
                allow_branch=False,
            )
            warnings = list(guardrails.warnings)
            if not dry_run and git_info.is_clean is None:
                return {
                    "error": "Git guardrails failed",
                    "errors": ["Could not determine working tree status; refusing a real push."],
                    "warnings": warnings,
                }
            if not guardrails.passed:
                return {
                    "error": "Git guardrails failed",
                    "errors": list(guardrails.errors),
                    "warnings": warnings,
                }
        else:
            warnings = ["Not a git repo — git guardrails skipped."]
    except Exception as e:  # noqa: BLE001
        return {"error": f"Could not prepare push: {_safe(str(e))}"}

    def go(ssh: Any, cfg: Any, root: Path) -> dict[str, Any]:
        """Operation body run over the connected SSH session."""
        from dango.platform.cloud.deployer import push_deploy

        try:
            result = push_deploy(
                ssh,
                root,
                cfg.droplet_ip,
                dry_run=dry_run,
                force=False,
                git_info=git_info if git_info.is_git_repo else None,
            )
        except Exception as e:  # noqa: BLE001
            msg = f"Push failed: {_safe(str(e))}"
            if not dry_run:
                msg += (
                    " A pre-deploy backup may have been created; rollback is CLI-only "
                    "(`dango remote rollback`)."
                )
            return {"error": msg}
        return _deploy_result(result, warnings)

    return _run(go)  # type: ignore[return-value]
