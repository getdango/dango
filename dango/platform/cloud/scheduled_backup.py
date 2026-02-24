"""dango/platform/cloud/scheduled_backup.py

Server-side scheduled backup for Dango cloud deployments.  Runs ON the
remote server (not via SSH from local).  Entry point::

    python -m dango.platform.cloud.scheduled_backup
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.exceptions import CloudProvisioningError

PROJECT_DIR = Path("/srv/dango/project")
BACKUP_DIR = Path("/srv/dango/backups/deploy")
HEALTH_FILE = Path("/srv/dango/backups/.backup_health.json")
LOCK_FILE = Path("/srv/dango/backups/.backup.lock")
VENV_PYTHON = "/srv/dango/venv/bin/python"
SPACES_PREFIX = "backups/"
DAILY_RETENTION = 7
WEEKLY_RETENTION = 4


@dataclass
class ScheduledBackupResult:
    """Result of a scheduled backup run."""

    archive_path: str = ""
    spaces_key: str = ""
    duration_seconds: float = 0.0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SpacesBackupInfo:
    """Metadata about a backup stored in Spaces."""

    key: str
    name: str
    size_bytes: int
    last_modified: str


def _acquire_backup_lock() -> Any:
    """Acquire exclusive file lock; returns open file handle (keep open to hold lock)."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")  # noqa: SIM115
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_fd.close()
        raise CloudProvisioningError(
            f"Another backup is already running (lock held on {LOCK_FILE})"
        ) from None
    return lock_fd


def _run_local(command: str, *, step: str, timeout: int = 120) -> str:
    """Run a shell command locally, raising on failure. Returns stdout."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise CloudProvisioningError(f"Step '{step}' timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise CloudProvisioningError(
            f"Step '{step}' failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _stop_services() -> None:
    """Stop dango-web and Metabase."""
    _run_local("systemctl stop dango-web || true", step="stop_web", timeout=60)
    _run_local(
        f"docker compose -f {PROJECT_DIR}/docker-compose.yml stop metabase 2>/dev/null || true",
        step="stop_metabase",
        timeout=120,
    )


def _start_services() -> None:
    """Start Metabase then dango-web."""
    subprocess.run(
        f"docker compose -f {PROJECT_DIR}/docker-compose.yml start metabase 2>/dev/null || true",
        shell=True,
        timeout=120,
    )
    subprocess.run("systemctl start dango-web || true", shell=True, timeout=60)


def _checkpoint_databases() -> list[str]:
    """Run CHECKPOINT on DuckDB and WAL checkpoint on auth.db. Returns warnings."""
    warnings: list[str] = []
    db_path = PROJECT_DIR / "data" / "warehouse.duckdb"
    if db_path.exists():
        try:
            import duckdb

            duck_conn = duckdb.connect(str(db_path))
            duck_conn.execute("CHECKPOINT")
            duck_conn.close()
        except Exception as exc:
            warnings.append(f"DuckDB checkpoint failed: {exc}")
    else:
        warnings.append("DuckDB warehouse not found — skipped checkpoint")
    auth_path = PROJECT_DIR / ".dango" / "auth.db"
    if auth_path.exists():
        try:
            import sqlite3

            sqlite_conn = sqlite3.connect(str(auth_path))
            sqlite_conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            sqlite_conn.close()
        except Exception as exc:
            warnings.append(f"Auth DB checkpoint failed: {exc}")
    else:
        warnings.append("Auth database not found — skipped checkpoint")
    return warnings


def _get_metabase_volume_path() -> str | None:
    """Discover the host path of the Metabase H2 Docker volume."""
    try:
        result = subprocess.run(
            "docker volume inspect project_metabase-data --format '{{.Mountpoint}}' 2>/dev/null",
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        pass
    return None


def _make_spaces_client(spaces_config: dict[str, Any]) -> Any:
    """Create a SpacesClient from config dict."""
    from dango.platform.cloud.spaces import SpacesClient

    return SpacesClient(bucket=spaces_config["bucket"], region=spaces_config["region"])


def _create_local_archive(backup_type: str) -> tuple[Path, dict[str, Any], list[str]]:
    """Create a tar.gz backup archive. Stops/restarts services via try/finally."""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_name = f"backup-{timestamp}"
    staging = Path(f"/tmp/{archive_name}")
    archive_path = BACKUP_DIR / f"{archive_name}.tar.gz"
    manifest_path = BACKUP_DIR / f"{archive_name}.json"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    _stop_services()
    try:
        warnings.extend(_checkpoint_databases())
        metabase_vol = _get_metabase_volume_path()
        staging.mkdir(parents=True, exist_ok=True)

        from dango.platform.cloud.backup import BACKUP_DIRS, BACKUP_FILES

        for fpath in BACKUP_FILES:
            src = PROJECT_DIR / fpath
            if src.exists():
                dest = staging / fpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                _run_local(f"cp '{src}' '{dest}'", step="copy_files")
        for dpath in BACKUP_DIRS:
            src = PROJECT_DIR / dpath
            if src.exists():
                dest = staging / dpath
                dest.mkdir(parents=True, exist_ok=True)
                _run_local(f"cp -r '{src}/.' '{dest}/'", step="copy_dirs")

        if metabase_vol:
            mb_staging = staging / "metabase"
            mb_staging.mkdir(parents=True, exist_ok=True)
            for h2 in ["metabase.db.mv.db", "metabase.db.trace.db"]:
                src = Path(metabase_vol) / h2
                if src.exists():
                    _run_local(f"cp '{src}' '{mb_staging}/'", step="copy_metabase")
        else:
            warnings.append("Metabase Docker volume not found — H2 backup skipped")

        files: list[dict[str, Any]] = []
        total_size = 0
        for f in staging.rglob("*"):
            if f.is_file():
                size = f.stat().st_size
                files.append({"path": str(f.relative_to(staging)), "size_bytes": size})
                total_size += size

        try:
            import dango

            dango_version = dango.__version__
        except Exception:
            dango_version = "unknown"

        manifest: dict[str, Any] = {
            "timestamp": timestamp,
            "backup_type": backup_type,
            "dango_version": dango_version,
            "files": files,
            "total_size_bytes": total_size,
        }
        manifest_json = json.dumps(manifest, indent=2)
        (staging / "manifest.json").write_text(manifest_json)
        _run_local(
            f"tar -czf '{archive_path}' -C /tmp '{archive_name}'",
            step="create_archive",
            timeout=600,
        )
        manifest_path.write_text(manifest_json)
    finally:
        subprocess.run(f"rm -rf '{staging}'", shell=True, timeout=30)
        _start_services()

    return archive_path, manifest, warnings


def _upload_to_spaces(archive_path: Path, spaces_config: dict[str, Any]) -> str:
    """Upload archive to Spaces. Returns the object key."""
    client = _make_spaces_client(spaces_config)
    key = f"{SPACES_PREFIX}{archive_path.name}"
    with open(archive_path, "rb") as f:
        client.upload(key, f, content_type="application/gzip")
    return key


def _verify_upload(spaces_config: dict[str, Any], key: str, local_size: int) -> bool:
    """Verify uploaded file size matches local archive."""
    try:
        client = _make_spaces_client(spaces_config)
        s3 = client._get_client()
        response: dict[str, Any] = s3.head_object(Bucket=client.bucket, Key=key)
        remote_size: int = response.get("ContentLength", 0)
        return remote_size == local_size
    except Exception:
        return False


def _load_spaces_config() -> dict[str, Any]:
    """Load Spaces config from cloud.yml on the server. Returns {bucket, region}."""
    cloud_yml = PROJECT_DIR / ".dango" / "cloud.yml"
    if not cloud_yml.exists():
        raise CloudProvisioningError("cloud.yml not found — Spaces not configured")
    try:
        import yaml
    except ImportError:
        raise CloudProvisioningError("PyYAML required to read cloud.yml") from None
    data: dict[str, Any] = yaml.safe_load(cloud_yml.read_text()) or {}
    spaces = data.get("spaces")
    if not spaces or not spaces.get("bucket"):
        raise CloudProvisioningError("Spaces not configured in cloud.yml. Set spaces.bucket first.")
    return {
        "bucket": spaces["bucket"],
        "region": spaces.get("region") or data.get("region", "nyc1"),
    }


def _apply_retention(spaces_config: dict[str, Any]) -> int:
    """Apply retention: keep 7 daily + 4 weekly backups. Returns number deleted."""
    client = _make_spaces_client(spaces_config)
    objects = client.list_objects(prefix=SPACES_PREFIX)
    archives = [o for o in objects if o.get("Key", "").endswith(".tar.gz")]
    if not archives:
        return 0

    now = datetime.now(tz=timezone.utc)
    dated: list[tuple[datetime, dict[str, Any]]] = []
    for obj in archives:
        name = obj["Key"].rsplit("/", 1)[-1]
        try:
            dt_str = name.replace("backup-", "").replace(".tar.gz", "")
            dt = datetime.strptime(dt_str, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
            dated.append((dt, obj))
        except ValueError:
            continue
    dated.sort(key=lambda x: x[0], reverse=True)

    keep_keys: set[str] = set()
    for dt, obj in dated:
        if (now - dt).days < DAILY_RETENTION:
            keep_keys.add(obj["Key"])

    weekly: dict[tuple[int, int], str] = {}
    for dt, obj in dated:
        if (now - dt).days >= DAILY_RETENTION:
            iso_year, iso_week, _ = dt.isocalendar()
            weekly[(iso_year, iso_week)] = obj["Key"]
    for wk in sorted(weekly.keys(), reverse=True)[:WEEKLY_RETENTION]:
        keep_keys.add(weekly[wk])

    deleted = 0
    for _dt, obj in dated:
        if obj["Key"] not in keep_keys:
            try:
                client.delete(obj["Key"])
                client.delete(obj["Key"].replace(".tar.gz", ".json"))
                deleted += 1
            except Exception:
                pass
    return deleted


def _write_health_status(success: bool, error: str | None = None) -> None:
    """Write backup health status to HEALTH_FILE."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if HEALTH_FILE.exists():
        try:
            existing = json.loads(HEALTH_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    if success:
        status: dict[str, Any] = {
            "last_run": now_iso,
            "last_success": now_iso,
            "last_error": None,
            "consecutive_failures": 0,
        }
    else:
        status = {
            "last_run": now_iso,
            "last_success": existing.get("last_success"),
            "last_error": error,
            "consecutive_failures": existing.get("consecutive_failures", 0) + 1,
        }
    HEALTH_FILE.write_text(json.dumps(status, indent=2))


def run_scheduled_backup() -> ScheduledBackupResult:
    """Run a full scheduled backup: lock -> archive -> upload -> retention -> health."""
    start_time = time.monotonic()
    result = ScheduledBackupResult()
    lock_fd = None
    try:
        lock_fd = _acquire_backup_lock()
        archive_path, _manifest, archive_warnings = _create_local_archive(backup_type="scheduled")
        result.archive_path = str(archive_path)
        result.warnings.extend(archive_warnings)

        try:
            spaces_config = _load_spaces_config()
        except CloudProvisioningError:
            result.warnings.append("Spaces not configured — local-only backup")
            _write_health_status(success=True)
            result.duration_seconds = round(time.monotonic() - start_time, 1)
            return result

        key = _upload_to_spaces(archive_path, spaces_config)
        result.spaces_key = key
        local_size = archive_path.stat().st_size
        if not _verify_upload(spaces_config, key, local_size):
            result.warnings.append("Upload verification failed — size mismatch")
        deleted = _apply_retention(spaces_config)
        if deleted > 0:
            result.warnings.append(f"Retention: deleted {deleted} old backup(s)")
        _write_health_status(success=True)
    except Exception as exc:
        result.error = str(exc)
        _write_health_status(success=False, error=str(exc))
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except OSError:
                pass
    result.duration_seconds = round(time.monotonic() - start_time, 1)
    return result


def list_spaces_backups(spaces_config: dict[str, Any]) -> list[SpacesBackupInfo]:
    """List backup archives in Spaces, sorted newest-first."""
    client = _make_spaces_client(spaces_config)
    backups: list[SpacesBackupInfo] = []
    for obj in client.list_objects(prefix=SPACES_PREFIX):
        key = obj.get("Key", "")
        if key.endswith(".tar.gz"):
            backups.append(
                SpacesBackupInfo(
                    key=key,
                    name=key.rsplit("/", 1)[-1],
                    size_bytes=obj.get("Size", 0),
                    last_modified=str(obj.get("LastModified", "")),
                )
            )
    backups.sort(key=lambda b: b.name, reverse=True)
    return backups


def download_from_spaces(spaces_config: dict[str, Any], key: str, local_path: Path) -> None:
    """Download a backup archive from Spaces to a local path."""
    client = _make_spaces_client(spaces_config)
    data = client.download(key)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)


def restore_from_spaces(spaces_config: dict[str, Any], key: str) -> None:
    """Download and restore a backup from Spaces. Stops/restarts services."""
    name = key.rsplit("/", 1)[-1]
    local_path = BACKUP_DIR / name
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    download_from_spaces(spaces_config, key, local_path)

    archive_name = name.replace(".tar.gz", "")
    staging = Path(f"/tmp/{archive_name}")
    _stop_services()
    try:
        _run_local(f"rm -rf '{staging}' && tar -xzf '{local_path}' -C /tmp", step="extract")
        from dango.platform.cloud.backup import BACKUP_DIRS, BACKUP_FILES

        for fpath in BACKUP_FILES:
            src = staging / fpath
            if src.exists():
                dest = PROJECT_DIR / fpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                _run_local(f"cp '{src}' '{dest}'", step="restore_files")
        for dpath in BACKUP_DIRS:
            src = staging / dpath
            if src.exists():
                dest = PROJECT_DIR / dpath
                dest.mkdir(parents=True, exist_ok=True)
                _run_local(f"cp -r '{src}/.' '{dest}/'", step="restore_dirs")

        metabase_vol = _get_metabase_volume_path()
        if metabase_vol and (staging / "metabase").exists():
            for h2 in ["metabase.db.mv.db", "metabase.db.trace.db"]:
                src = staging / "metabase" / h2
                if src.exists():
                    _run_local(f"cp '{src}' '{metabase_vol}/'", step="restore_metabase")
        _run_local("chown -R dango:dango /srv/dango/project", step="fix_ownership")
        subprocess.run(f"rm -rf '{staging}'", shell=True, timeout=30)
    finally:
        _start_services()


def enable_scheduled_backup() -> bool:
    """Enable the systemd backup timer. Returns True on success."""
    r = subprocess.run(
        "systemctl enable --now dango-backup.timer",
        shell=True,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def disable_scheduled_backup() -> bool:
    """Disable the systemd backup timer. Returns True on success."""
    r = subprocess.run(
        "systemctl disable --now dango-backup.timer",
        shell=True,
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def is_scheduled_backup_enabled() -> bool:
    """Check if the backup timer is active."""
    r = subprocess.run(
        "systemctl is-active dango-backup.timer",
        shell=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return r.returncode == 0 and "active" in r.stdout


if __name__ == "__main__":
    backup_result = run_scheduled_backup()
    if backup_result.error:
        print(f"Backup failed: {backup_result.error}", file=sys.stderr)
        sys.exit(1)
    print(f"Backup complete in {backup_result.duration_seconds}s: {backup_result.archive_path}")
    for w in backup_result.warnings:
        print(f"  Warning: {w}")
    sys.exit(0)
