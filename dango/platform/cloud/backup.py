"""dango/platform/cloud/backup.py

SSH-based backup and rollback for Dango cloud deployments.

Creates pre-deploy snapshots of all project data on the remote server.
Services are stopped during backup to guarantee consistency.
All functions require an already-connected ``SSHManager`` (as root).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger
from dango.platform.docker import get_compose_project_name

_logger = get_logger(__name__)

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

PROJECT_DIR = "/srv/dango/project"
BACKUP_DIR = "/srv/dango/backups/deploy"
VENV_PYTHON = "/srv/dango/venv/bin/python"
MAX_LOCAL_BACKUPS = 14

# Must match DockerManager.compose_project_name for /srv/dango/project
_COMPOSE_PROJECT = get_compose_project_name(PROJECT_DIR)

#: Files to back up, relative to PROJECT_DIR.
BACKUP_FILES = [
    "data/warehouse.duckdb",
    ".dango/auth.db",
    ".dango/project.yml",
    ".dango/sources.yml",
    ".dango/cloud.yml",
    ".dango/metabase.yml",
    ".dango/logs/audit.jsonl",
    ".dlt/secrets.toml",
    "dbt/profiles.yml",
    "dbt/dbt_project.yml",
    "dbt/packages.yml",
    ".env",
]

#: Directories to back up (recursively), relative to PROJECT_DIR.
BACKUP_DIRS = [
    ".dlt/pipelines",
    "dbt/models",
    "dbt/macros",
    "dbt/seeds",
    "custom_sources",
    "data/seeds",
]


@dataclass(frozen=True)
class BackupManifest:
    """Metadata about a backup archive."""

    timestamp: str
    backup_type: str
    dango_version: str
    files: list[dict[str, Any]] = field(default_factory=list)
    total_size_bytes: int = 0
    git_commit: str | None = None
    git_branch: str | None = None


@dataclass
class BackupResult:
    """Result returned by :func:`create_backup`."""

    archive_path: str
    manifest_path: str
    manifest: BackupManifest
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)


@dataclass
class RestoreResult:
    """Result returned by :func:`rollback`."""

    restored_from: str
    services_restarted: bool
    health_check_passed: bool
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)


def _run_checked(ssh: SSHManager, command: str, *, step: str, timeout: int = 120) -> str:
    """Run *command* via SSH, raising ``CloudProvisioningError`` on failure."""
    result = ssh.exec_command(command, timeout=timeout)
    if not result.success:
        raise CloudProvisioningError(
            f"Backup step '{step}' failed (exit {result.exit_code}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _notify(callback: Callable[[str, str], None] | None, step: str, status: str) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def _check_disk_space(ssh: SSHManager, required_mb: int = 500) -> None:
    """Raise ``CloudProvisioningError`` if <*required_mb* MB free on /srv."""
    stdout = _run_checked(ssh, "df -m /srv/dango | tail -1", step="check_disk_space")
    parts = stdout.split()
    if len(parts) >= 4:
        try:
            available = int(parts[3])
        except ValueError:
            return
        if available < required_mb:
            raise CloudProvisioningError(
                f"Insufficient disk space: {available} MB available, "
                f"need at least {required_mb} MB for backup"
            )


def stop_services(ssh: SSHManager) -> None:
    """Stop dango-web and Metabase to ensure no concurrent writes.

    Best-effort — does not raise if services are already stopped.
    """
    result = ssh.exec_command("systemctl stop dango-web", timeout=60)
    if result.exit_code != 0:
        _logger.warning("service_stop_failed", service="dango-web", stderr=result.stderr)
    result = ssh.exec_command(
        f"COMPOSE_PROJECT_NAME={_COMPOSE_PROJECT} docker compose -f {PROJECT_DIR}/docker-compose.yml stop metabase 2>/dev/null || true",
        timeout=120,
    )
    if result.exit_code != 0:
        _logger.warning("service_stop_failed", service="metabase", stderr=result.stderr)


def start_services(ssh: SSHManager) -> None:
    """Start Metabase then dango-web (reverse order of stop)."""
    result = ssh.exec_command(
        f"COMPOSE_PROJECT_NAME={_COMPOSE_PROJECT} docker compose -f {PROJECT_DIR}/docker-compose.yml start metabase 2>/dev/null || true",
        timeout=120,
    )
    if result.exit_code != 0:
        _logger.warning("service_start_failed", service="metabase", stderr=result.stderr)
    result = ssh.exec_command("systemctl start dango-web", timeout=60)
    if result.exit_code != 0:
        _logger.warning("service_start_failed", service="dango-web", stderr=result.stderr)


def _checkpoint_duckdb(ssh: SSHManager) -> bool:
    """Run CHECKPOINT on DuckDB.  Returns False if file missing."""
    db_path = f"{PROJECT_DIR}/data/warehouse.duckdb"
    if not ssh.exec_command(f"test -f {db_path}").success:
        return False
    _run_checked(
        ssh,
        f"{VENV_PYTHON} -c \"import duckdb; c=duckdb.connect('{db_path}'); "
        f"c.execute('CHECKPOINT'); c.close()\"",
        step="checkpoint_duckdb",
        timeout=120,
    )
    return True


def _checkpoint_auth_db(ssh: SSHManager) -> bool:
    """Run WAL checkpoint on auth.db.  Returns False if file missing."""
    db_path = f"{PROJECT_DIR}/.dango/auth.db"
    if not ssh.exec_command(f"test -f {db_path}").success:
        return False
    _run_checked(
        ssh,
        f"{VENV_PYTHON} -c \"import sqlite3; c=sqlite3.connect('{db_path}'); "
        f"c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()\"",
        step="checkpoint_auth_db",
        timeout=30,
    )
    return True


def _get_metabase_volume_path(ssh: SSHManager) -> str | None:
    """Discover the host path of the Metabase H2 Docker volume."""
    result = ssh.exec_command(
        "docker volume inspect project_metabase-data --format '{{.Mountpoint}}' 2>/dev/null"
    )
    return result.stdout.strip() if result.success and result.stdout.strip() else None


def _create_archive(
    ssh: SSHManager,
    timestamp: str,
    metabase_vol_path: str | None,
    backup_type: str,
    *,
    git_commit: str | None = None,
    git_branch: str | None = None,
) -> tuple[str, BackupManifest]:
    """Create a tar.gz archive in BACKUP_DIR.  Returns (archive_path, manifest)."""
    archive_name = f"backup-{timestamp}"
    staging = f"/tmp/{archive_name}"
    archive_path = f"{BACKUP_DIR}/{archive_name}.tar.gz"

    try:
        _run_checked(ssh, f"mkdir -p {BACKUP_DIR} {staging}", step="create_staging")

        # Build copy commands for all files/dirs/metabase
        copy_cmds: list[str] = []
        for fpath in BACKUP_FILES:
            src = f"{PROJECT_DIR}/{fpath}"
            dest_dir = f"{staging}/{'/'.join(fpath.split('/')[:-1])}" if "/" in fpath else staging
            copy_cmds.append(f"mkdir -p {dest_dir} && cp {src} {dest_dir}/ 2>/dev/null || true")
        for dpath in BACKUP_DIRS:
            src, dest = f"{PROJECT_DIR}/{dpath}", f"{staging}/{dpath}"
            copy_cmds.append(f"mkdir -p {dest} && cp -r {src}/. {dest}/ 2>/dev/null || true")
        if metabase_vol_path:
            copy_cmds.append(
                f"mkdir -p {staging}/metabase"
                f" && cp {metabase_vol_path}/metabase.db.mv.db {staging}/metabase/ 2>/dev/null || true"
                f" && cp {metabase_vol_path}/metabase.db.trace.db {staging}/metabase/ 2>/dev/null || true"
            )
        _run_checked(ssh, " && ".join(copy_cmds), step="copy_files", timeout=300)

        # Get version + file info for manifest
        ver = ssh.exec_command(f'{VENV_PYTHON} -c "import dango; print(dango.__version__)"')
        dango_version = ver.stdout.strip() if ver.success else "unknown"

        fi = ssh.exec_command(f"find {staging} -type f -exec stat --format='%n %s' {{}} \\;")
        files: list[dict[str, Any]] = []
        total_size = 0
        if fi.success:
            for line in fi.stdout.strip().splitlines():
                parts = line.rsplit(" ", 1)
                if len(parts) == 2:
                    try:
                        size = int(parts[1])
                    except ValueError:
                        size = 0
                    files.append(
                        {"path": parts[0].replace(f"{staging}/", "", 1), "size_bytes": size}
                    )
                    total_size += size

        manifest = BackupManifest(
            timestamp=timestamp,
            backup_type=backup_type,
            dango_version=dango_version,
            files=files,
            total_size_bytes=total_size,
            git_commit=git_commit,
            git_branch=git_branch,
        )
        manifest_json = json.dumps(asdict(manifest), indent=2)
        _run_checked(
            ssh,
            f"cat > {staging}/manifest.json << 'MANIFEST_EOF'\n{manifest_json}\nMANIFEST_EOF",
            step="write_manifest",
        )
        _run_checked(
            ssh,
            f"tar -czf {archive_path} -C /tmp {archive_name}",
            step="create_archive",
            timeout=600,
        )
        _run_checked(
            ssh,
            f"cp {staging}/manifest.json {BACKUP_DIR}/{archive_name}.json",
            step="copy_manifest",
        )
    finally:
        ssh.exec_command(f"rm -rf {staging}")  # cleanup: silent OK

    return archive_path, manifest


def verify_health(ssh: SSHManager, timeout: int = 90) -> bool:
    """Poll the health endpoint until it responds OK or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ssh.exec_command("curl -sf http://localhost:8800/api/health", timeout=10).success:
            return True
        time.sleep(5)
    return False


def create_backup(
    ssh: SSHManager,
    *,
    backup_type: str = "pre-deploy",
    restart_services: bool = True,
    on_progress: Callable[[str, str], None] | None = None,
    git_commit: str | None = None,
    git_branch: str | None = None,
) -> BackupResult:
    """Create a backup of all project data on the remote server.

    Stops services, checkpoints databases, creates a tar.gz archive,
    then (optionally) restarts services.

    Args:
        ssh: Connected SSHManager (as root).
        backup_type: Label for the backup manifest (e.g. ``"pre-deploy"``).
        restart_services: If False, services remain stopped after backup.
            Caller is responsible for restarting them.  Useful when the
            caller needs services to stay down (e.g. ``push_deploy``).
        on_progress: Optional ``(step, status)`` callback.
    """
    start_time = time.monotonic()
    warnings: list[str] = []
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    _notify(on_progress, "check_disk_space", "running")
    _check_disk_space(ssh)
    _notify(on_progress, "check_disk_space", "done")

    _notify(on_progress, "stop_services", "running")
    stop_services(ssh)
    _notify(on_progress, "stop_services", "done")

    try:
        _notify(on_progress, "checkpoint_databases", "running")
        if not _checkpoint_duckdb(ssh):
            warnings.append("DuckDB warehouse not found — skipped checkpoint")
        if not _checkpoint_auth_db(ssh):
            warnings.append("Auth database not found — skipped checkpoint")
        _notify(on_progress, "checkpoint_databases", "done")

        _notify(on_progress, "get_metabase_volume", "running")
        metabase_vol = _get_metabase_volume_path(ssh)
        if metabase_vol is None:
            warnings.append("Metabase Docker volume not found — H2 backup skipped")
        _notify(on_progress, "get_metabase_volume", "done")

        _notify(on_progress, "create_archive", "running")
        archive_path, manifest = _create_archive(
            ssh,
            timestamp,
            metabase_vol,
            backup_type,
            git_commit=git_commit,
            git_branch=git_branch,
        )
        _notify(on_progress, "create_archive", "done")
    finally:
        if restart_services:
            _notify(on_progress, "start_services", "running")
            start_services(ssh)
            _notify(on_progress, "start_services", "done")

    _notify(on_progress, "rotate_backups", "running")
    rotate_local_backups(ssh)
    _notify(on_progress, "rotate_backups", "done")

    return BackupResult(
        archive_path=archive_path,
        manifest_path=archive_path.replace(".tar.gz", ".json"),
        manifest=manifest,
        duration_seconds=round(time.monotonic() - start_time, 1),
        warnings=warnings,
    )


def list_local_backups(ssh: SSHManager) -> list[dict[str, Any]]:
    """List backup archives on the remote server, newest first."""
    result = ssh.exec_command(f"ls -1t {BACKUP_DIR}/*.tar.gz 2>/dev/null || true")
    if not result.success or not result.stdout.strip():
        return []

    backups: list[dict[str, Any]] = []
    for line in result.stdout.strip().splitlines():
        path = line.strip()
        if not path:
            continue
        name = path.rsplit("/", 1)[-1]
        size_result = ssh.exec_command(f"stat --format='%s' {path} 2>/dev/null")
        try:
            size = int(size_result.stdout.strip()) if size_result.success else 0
        except ValueError:
            size = 0
        date = name[7:22] if name.startswith("backup-") and len(name) >= 22 else ""
        backups.append({"name": name, "path": path, "size_bytes": size, "date": date})
    return backups


def rollback(
    ssh: SSHManager,
    *,
    backup_path: str | None = None,
    on_progress: Callable[[str, str], None] | None = None,
) -> RestoreResult:
    """Restore from a backup archive on the remote server.

    Uses the most recent backup if *backup_path* is ``None``.
    """
    start_time = time.monotonic()

    _notify(on_progress, "find_backup", "running")
    if backup_path is None:
        backups = list_local_backups(ssh)
        if not backups:
            raise CloudProvisioningError("No backups found to restore from")
        backup_path = backups[0]["path"]
    else:
        if not ssh.exec_command(f"test -f {backup_path}").success:
            raise CloudProvisioningError(f"Backup archive not found: {backup_path}")
    _notify(on_progress, "find_backup", "done")

    return restore_from_archive(ssh, backup_path, on_progress=on_progress, _start_time=start_time)


def restore_from_archive(
    ssh: SSHManager,
    archive_path: str,
    *,
    on_progress: Callable[[str, str], None] | None = None,
    _start_time: float | None = None,
) -> RestoreResult:
    """Low-level restore from an archive path.  Used by rollback() and Spaces restore."""
    if _start_time is None:
        _start_time = time.monotonic()
    warnings: list[str] = []

    manifest_path = archive_path.replace(".tar.gz", ".json")
    manifest_result = ssh.exec_command(f"cat {manifest_path} 2>/dev/null")
    if manifest_result.success and manifest_result.stdout.strip():
        _notify(on_progress, "read_manifest", "done")

    _notify(on_progress, "stop_services", "running")
    stop_services(ssh)
    _notify(on_progress, "stop_services", "done")

    services_restarted = False
    try:
        archive_name = archive_path.rsplit("/", 1)[-1].replace(".tar.gz", "")
        staging = f"/tmp/{archive_name}"

        _notify(on_progress, "extract_archive", "running")
        _run_checked(
            ssh,
            f"rm -rf {staging} && tar -xzf {archive_path} -C /tmp",
            step="extract_archive",
            timeout=300,
        )
        _notify(on_progress, "extract_archive", "done")

        _notify(on_progress, "restore_files", "running")
        restore_cmds: list[str] = []
        for fpath in BACKUP_FILES:
            src = f"{staging}/{fpath}"
            dest_dir = (
                f"{PROJECT_DIR}/{'/'.join(fpath.split('/')[:-1])}" if "/" in fpath else PROJECT_DIR
            )
            restore_cmds.append(
                f"test -f {src} && (mkdir -p {dest_dir} && cp {src} {dest_dir}/) || true"
            )
        for dpath in BACKUP_DIRS:
            src, dest = f"{staging}/{dpath}", f"{PROJECT_DIR}/{dpath}"
            restore_cmds.append(
                f"test -d {src} && (mkdir -p {dest} && cp -r {src}/. {dest}/) || true"
            )
        _run_checked(ssh, " && ".join(restore_cmds), step="restore_files", timeout=300)
        _notify(on_progress, "restore_files", "done")

        _notify(on_progress, "restore_metabase", "running")
        metabase_vol = _get_metabase_volume_path(ssh)
        if metabase_vol:
            for h2 in ["metabase.db.mv.db", "metabase.db.trace.db"]:
                ssh.exec_command(
                    f"test -d {staging}/metabase && cp {staging}/metabase/{h2} {metabase_vol}/ 2>/dev/null || true"
                )
        else:
            warnings.append("Metabase Docker volume not found — H2 restore skipped")
        _notify(on_progress, "restore_metabase", "done")

        _notify(on_progress, "fix_ownership", "running")
        _run_checked(ssh, "chown -R dango:dango /srv/dango/project", step="fix_ownership")
        _notify(on_progress, "fix_ownership", "done")

        ssh.exec_command(f"rm -rf {staging}")  # cleanup: silent OK
    finally:
        _notify(on_progress, "start_services", "running")
        start_services(ssh)
        services_restarted = True
        _notify(on_progress, "start_services", "done")

    _notify(on_progress, "verify_health", "running")
    health_ok = verify_health(ssh)
    if not health_ok:
        warnings.append("Health check did not pass within 90 seconds")
    _notify(on_progress, "verify_health", "done")

    return RestoreResult(
        restored_from=archive_path,
        services_restarted=services_restarted,
        health_check_passed=health_ok,
        duration_seconds=round(time.monotonic() - _start_time, 1),
        warnings=warnings,
    )


def rotate_local_backups(ssh: SSHManager, keep: int = MAX_LOCAL_BACKUPS) -> int:
    """Delete old backup archives beyond the retention limit."""
    result = ssh.exec_command(f"ls -1t {BACKUP_DIR}/*.tar.gz 2>/dev/null || true")
    if not result.success or not result.stdout.strip():
        return 0
    archives = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    if len(archives) <= keep:
        return 0
    deleted = 0
    for archive in archives[keep:]:
        ssh.exec_command(
            f"rm -f {archive} {archive.replace('.tar.gz', '.json')}"
        )  # cleanup: silent OK
        deleted += 1
    return deleted
