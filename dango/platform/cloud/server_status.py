"""dango/platform/cloud/server_status.py

Server status collection for Dango cloud deployments.

Gathers system resource metrics (CPU, RAM, disk), service status, and
application data (DuckDB size, sync history, versions) via SSH commands.
All collection is fault-tolerant — missing data returns ``None`` fields,
never raises.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from typing import Any

# SSHManager is lazy-imported to keep module importable without paramiko.


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceInfo:
    """Status of a single service on the remote server."""

    name: str
    status: str  # "running", "stopped", "not-found", etc.


@dataclass(frozen=True)
class ServerStatus:
    """Snapshot of remote server health and application state.

    All optional fields default to ``None`` for graceful missing-data handling.
    """

    # Resources
    cpu_usage_pct: float | None = None
    ram_total_mb: int | None = None
    ram_used_mb: int | None = None
    disk_total_mb: int | None = None
    disk_used_mb: int | None = None
    disk_available_mb: int | None = None

    # Services
    services: list[ServiceInfo] = field(default_factory=list)

    # Application data
    duckdb_size_bytes: int | None = None
    dango_version: str | None = None
    last_deploy: str | None = None
    last_backup: str | None = None
    last_sync_per_source: dict[str, str] = field(default_factory=dict)

    # Deployment version info (from deployments.jsonl)
    deployed_git_commit: str | None = None
    deployed_git_branch: str | None = None
    deployed_at: str | None = None
    deployed_by: str | None = None


# ---------------------------------------------------------------------------
# SSH-based collection
# ---------------------------------------------------------------------------

_DUCKDB_PATH = "/srv/dango/project/data/warehouse.duckdb"
_VENV_PYTHON = "/srv/dango/venv/bin/python3"
_SYNC_HISTORY = "/srv/dango/project/.dango/state/sync_history.jsonl"
_DEPLOY_TIMESTAMP = "/srv/dango/project/.dango/state/deploy_timestamp"
_DEPLOY_JOURNAL = "/srv/dango/project/.dango/state/deployments.jsonl"
_BACKUP_DIR = "/srv/dango/backups/deploy"
_SYNC_HISTORY_TAIL = 50  # Recent lines to read — sufficient for per-source latest timestamps


def collect_server_status(ssh: Any, cloud_config: Any) -> ServerStatus:
    """Collect server status via SSH.

    All commands use ``2>/dev/null`` and check ``result.success`` — missing
    data returns ``None``, never errors.

    Args:
        ssh: Connected ``SSHManager`` instance.
        cloud_config: ``CloudConfig`` with deployment metadata.

    Returns:
        ``ServerStatus`` snapshot with available metrics.
    """
    deploy_info = _get_deployment_info(ssh)
    return ServerStatus(
        cpu_usage_pct=_get_cpu_usage(ssh),
        ram_total_mb=_get_ram(ssh, "total"),
        ram_used_mb=_get_ram(ssh, "used"),
        disk_total_mb=_get_disk(ssh, "total"),
        disk_used_mb=_get_disk(ssh, "used"),
        disk_available_mb=_get_disk(ssh, "available"),
        services=_get_services(ssh),
        duckdb_size_bytes=_get_duckdb_size(ssh),
        dango_version=_get_dango_version(ssh),
        last_deploy=_get_last_deploy(ssh),
        last_backup=_get_last_backup(ssh),
        last_sync_per_source=_get_sync_history(ssh),
        deployed_git_commit=deploy_info.get("git_commit"),
        deployed_git_branch=deploy_info.get("git_branch"),
        deployed_at=deploy_info.get("deployed_at"),
        deployed_by=deploy_info.get("deployed_by"),
    )


def _get_deployment_info(ssh: Any) -> dict[str, str | None]:
    """Read last entry from remote deployments.jsonl for version info."""
    from dango.platform.cloud.deploy_journal import get_latest_deployment

    entry = get_latest_deployment(ssh)
    if not entry:
        return {}
    return {
        "git_commit": entry.get("git_commit"),
        "git_branch": entry.get("git_branch"),
        "deployed_at": entry.get("timestamp"),
        "deployed_by": entry.get("deployer"),
    }


def _get_cpu_usage(ssh: Any) -> float | None:
    """Parse CPU usage from ``top -bn1``."""
    result = ssh.exec_command("top -bn1 -p0 2>/dev/null | grep '%Cpu'")
    if not result.success or not result.stdout.strip():
        return None
    try:
        # Format: %Cpu(s):  1.5 us,  0.3 sy, ... 97.2 id, ...
        for part in result.stdout.split(","):
            if part.strip().endswith("id"):
                idle = float(part.strip().split()[0])
                return round(100.0 - idle, 1)
    except (ValueError, IndexError):
        pass
    return None


def _get_ram(ssh: Any, field_name: str) -> int | None:
    """Parse RAM from ``free -m``.

    Args:
        field_name: "total" or "used".
    """
    result = ssh.exec_command("free -m 2>/dev/null")
    if not result.success or not result.stdout.strip():
        return None
    try:
        for line in result.stdout.splitlines():
            if line.startswith("Mem:"):
                parts = line.split()
                if field_name == "total":
                    return int(parts[1])
                if field_name == "used":
                    return int(parts[2])
    except (ValueError, IndexError):
        pass
    return None


def _get_disk(ssh: Any, field_name: str) -> int | None:
    """Parse disk usage from ``df -BM``.

    Args:
        field_name: "total", "used", or "available".
    """
    result = ssh.exec_command("df -BM /srv/dango 2>/dev/null")
    if not result.success or not result.stdout.strip():
        return None
    try:
        lines = result.stdout.strip().splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            # Columns: Filesystem 1M-blocks Used Available Use% Mounted
            idx = {"total": 1, "used": 2, "available": 3}.get(field_name)
            if idx is not None:
                return int(parts[idx].rstrip("M"))
    except (ValueError, IndexError):
        pass
    return None


def _get_services(ssh: Any) -> list[ServiceInfo]:
    """Check status of dango-web, caddy, and metabase container."""
    services: list[ServiceInfo] = []

    for unit in ("dango-web", "caddy"):
        result = ssh.exec_command(f"systemctl is-active {unit} 2>/dev/null")
        status = result.stdout.strip() if result.success else "not-found"
        if not status:
            status = "not-found"
        services.append(ServiceInfo(name=unit, status=status))

    # Metabase runs as a Docker container — use docker ps with name filter
    # to match Compose v2 naming (e.g. dango-project-metabase-1)
    result = ssh.exec_command(
        "docker ps -a --filter 'name=metabase' --format '{{.Status}}' 2>/dev/null"
    )
    raw = result.stdout.strip() if result.success else ""
    if raw:
        status = "running" if raw.lower().startswith("up") else "stopped"
    else:
        status = "not-found"
    services.append(ServiceInfo(name="metabase", status=status))

    return services


def _get_duckdb_size(ssh: Any) -> int | None:
    """Get DuckDB file size in bytes."""
    result = ssh.exec_command(f"stat --format=%s {_DUCKDB_PATH} 2>/dev/null")
    if not result.success or not result.stdout.strip():
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def _get_dango_version(ssh: Any) -> str | None:
    """Get installed dango version from remote venv."""
    result = ssh.exec_command(
        f'{_VENV_PYTHON} -c "import dango; print(dango.__version__)" 2>/dev/null'
    )
    if not result.success or not result.stdout.strip():
        return None
    return str(result.stdout.strip())


def _get_last_deploy(ssh: Any) -> str | None:
    """Read last deploy timestamp."""
    result = ssh.exec_command(f"cat {_DEPLOY_TIMESTAMP} 2>/dev/null")
    if not result.success or not result.stdout.strip():
        return None
    return str(result.stdout.strip())


def _get_last_backup(ssh: Any) -> str | None:
    """Get most recent backup filename (contains timestamp)."""
    result = ssh.exec_command(f"ls -t {_BACKUP_DIR} 2>/dev/null | head -1")
    if not result.success or not result.stdout.strip():
        return None
    return str(result.stdout.strip())


def _get_sync_history(ssh: Any) -> dict[str, str]:
    """Parse per-source last sync timestamps from sync_history.jsonl."""
    result = ssh.exec_command(f"cat {_SYNC_HISTORY} 2>/dev/null | tail -{_SYNC_HISTORY_TAIL}")
    if not result.success or not result.stdout.strip():
        return {}
    latest: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        try:
            entry: dict[str, Any] = json.loads(line)
            source = entry.get("source", "")
            timestamp = entry.get("timestamp", entry.get("completed_at", ""))
            if source and timestamp:
                latest[source] = timestamp
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
    return latest


# ---------------------------------------------------------------------------
# PyPI version check (runs locally, not via SSH)
# ---------------------------------------------------------------------------


def check_latest_pypi_version(*, include_pre: bool = False) -> str | None:
    """Check the latest ``getdango`` version on PyPI.

    Args:
        include_pre: If ``True``, include pre-release versions (alpha, beta, rc).
            When ``False`` (default), returns the latest stable version only.

    Returns the version string (e.g. ``"1.0.0"``) or ``None`` on any failure
    (network error, timeout, unexpected response format).
    """
    try:
        import httpx  # noqa: E402
    except ImportError:
        return None

    try:
        response = httpx.get(
            "https://pypi.org/pypi/getdango/json",
            timeout=5.0,
            follow_redirects=True,
        )
        if response.status_code == 200:
            data: dict[str, Any] = response.json()
            if include_pre:
                from packaging.version import Version

                releases = data.get("releases", {})
                versions = [v for v in releases if releases[v]]
                if versions:
                    return str(max(versions, key=Version))
                # Fall through to stable version if no releases found
            version: str | None = data.get("info", {}).get("version")
            return version
    except Exception:  # noqa: BLE001 — intentionally broad for resilience
        pass
    return None


# ---------------------------------------------------------------------------
# Local resource usage (for the web endpoint running ON the cloud server)
# ---------------------------------------------------------------------------


def get_local_resource_usage() -> dict[str, Any]:
    """Collect local resource metrics without psutil.

    Uses ``shutil.disk_usage()`` for disk and ``/proc/`` for RAM/CPU on Linux.
    Returns ``None`` values for metrics that cannot be collected (e.g. on macOS).
    """
    result: dict[str, Any] = {
        "cpu_usage_pct": None,
        "ram_total_mb": None,
        "ram_used_mb": None,
        "disk_total_mb": None,
        "disk_used_mb": None,
        "disk_free_mb": None,
    }

    # Disk — works on all platforms
    try:
        usage = shutil.disk_usage("/srv/dango")
        result["disk_total_mb"] = usage.total // (1024 * 1024)
        result["disk_used_mb"] = usage.used // (1024 * 1024)
        result["disk_free_mb"] = usage.free // (1024 * 1024)
    except OSError:
        pass

    # RAM — Linux only (/proc/meminfo)
    try:
        with open("/proc/meminfo") as f:
            meminfo: dict[str, int] = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    meminfo[key] = int(parts[1])  # kB
            total_kb = meminfo.get("MemTotal", 0)
            available_kb = meminfo.get("MemAvailable", 0)
            if total_kb:
                result["ram_total_mb"] = total_kb // 1024
                result["ram_used_mb"] = (total_kb - available_kb) // 1024
    except (OSError, ValueError):
        pass

    # CPU — Linux only (/proc/loadavg, 1-min load average normalized by CPU count)
    try:
        import os

        with open("/proc/loadavg") as f:
            load_1min = float(f.readline().split()[0])
        cpu_count = os.cpu_count() or 1
        # Normalize load average to percentage (capped at 100%)
        result["cpu_usage_pct"] = round(min(100.0, 100.0 * load_1min / cpu_count), 1)
    except (OSError, ValueError, IndexError):
        pass

    return result
