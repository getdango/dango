"""dango/platform/cloud/backup.py

SSH-based backup and rollback for Dango cloud deployments.

Creates pre-deploy snapshots of all project data on the remote server.
Services are stopped during backup to guarantee consistency.
All functions require an already-connected ``SSHManager`` (as root).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger
from dango.platform.cloud.backup_restore import (
    AS_SERVICE_USER,
    AUTH_CONFIG_FILE,
    AUTH_CONFIG_MAX_BYTES,
    AUTH_CONFIG_OVERSIZED_WARNING,
    AUTH_CONFIG_SKIPPED_WARNING,
    RESTORE_PRIVATE_ROOT,
    SANITIZED_METABASE_NAME,
    SERVICE_USER,
    auth_config_keeps_auth_on,
    backup_dir_cmd,
    backup_file_cmd,
    clear_metabase_lock_state_cmd,
    copy_file_cmd,
    extract_checked_archive,
    hand_staging_to_service_user_cmd,
    private_dir_cmd,
    prune_backup_staging_cmd,
    restore_backup_dir_cmd,
    restore_file_cmd,
    restore_volume_file_cmd,
    set_aside_pipeline_state_cmd,
    skipped_from_output,
    skipped_warning,
    wal_sidecar_names,
)
from dango.platform.cloud.compose_helper import (
    CALLER_MARGIN,
    MARKER_TEST,
    ROOT_LOCK_WAIT,
    VERB_BUDGET,
    server_compose_cmd,
)
from dango.platform.cloud.runtime_ownership import ROOT_PYTHON
from dango.platform.compose_exec import EXIT_BUSY
from dango.platform.docker import _legacy_path_hash

_logger = get_logger(__name__)

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

PROJECT_DIR = "/srv/dango/project"
BACKUP_DIR = "/srv/dango/backups/deploy"
VENV_PYTHON = "/srv/dango/venv/bin/python"
#: The same interpreter as the service user, for work on files that user owns.
SERVICE_PYTHON = f"cd / && runuser -u dango -- {VENV_PYTHON} -I"
MAX_LOCAL_BACKUPS = 1
#: Root-only parent of the generated per-restore upload, staging and scratch directories.
RESTORE_STAGING_ROOT = RESTORE_PRIVATE_ROOT
#: Parent of the private (``mktemp -d``, 0700) per-backup staging directories.
BACKUP_STAGING_ROOT = "/tmp"
#: Shown when the compose helper reports that another Docker operation holds its lock.
DOCKER_BUSY_MESSAGE = (
    "Another Docker operation is in progress on the server; wait a few minutes and retry."
)


class ComposeStopError(CloudProvisioningError):
    """Metabase could not be stopped through the helper; the services are running again."""


class DockerBusyError(ComposeStopError):
    """The compose helper was busy; the services were left (or put back) running."""


def root_helper_timeout(verb: str) -> int:
    """SSH timeout for a root-side helper call: lock wait + verb budget + margin."""
    return ROOT_LOCK_WAIT + VERB_BUDGET[verb] + CALLER_MARGIN


# A project id is uuid4().hex; only the first 8 characters become the compose project name,
# and that name is interpolated into remote shell commands, so its shape is enforced here.
_COMPOSE_ID_PREFIX_RE = re.compile(r"[a-f0-9]{8}")


def get_remote_compose_project_name(ssh: SSHManager, project_dir: str = PROJECT_DIR) -> str:
    """Return the Docker Compose project name for ``project_dir`` on the
    connected remote server.

    1.0.8-Q9: the server's real compose project name comes from its own
    persisted ``project.id`` (read here via SSH, since the remote id isn't
    knowable from the local machine — a module-level constant computed at
    import time, as this used to be, can't see it and silently goes stale
    the moment a freshly-deployed project's real (random) id diverges from
    the legacy path hash of the literal string ``project_dir``).

    Falls back to the deterministic legacy path hash if project.yml can't
    be read remotely or has no persisted id yet — this matches what the
    server's own ``DockerManager`` migration would adopt if legacy
    containers already exist there, and is a safe, non-random default even
    when they don't, since this is a read-only diagnostic/lifecycle helper,
    not the authoritative migration decision (that only ever happens inside
    ``DockerManager.start_services()``/``stop_services()`` on the server
    itself, via ``dango serve``).
    """
    try:
        from dango.platform.cloud.project_files import read_project_file

        text = read_project_file(ssh, f"{project_dir}/.dango/project.yml")
        if text.strip():
            import yaml

            data = yaml.safe_load(text) or {}
            project_id = data.get("project", {}).get("id")
            if isinstance(project_id, str) and project_id:
                prefix = project_id[:8]
                if _COMPOSE_ID_PREFIX_RE.fullmatch(prefix):
                    return f"dango-{prefix}"
                _logger.warning("remote_project_id_rejected", project_dir=project_dir)
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "remote_project_id_unreadable_using_legacy_hash",
            project_dir=project_dir,
            error_type=type(exc).__name__,
        )
    return f"dango-{_legacy_path_hash(project_dir)}"


#: Files to back up, relative to PROJECT_DIR.
#: NOTE: See also file_sync.py SYNC_CONFIG_FILES/SYNC_DBT_DIRS/SYNC_EXTRA_DIRS
#: for what gets synced to remote. Backup includes additional server-only files
#: (dbt/profiles.yml) that are never synced.  The warehouse is backed up with the
#: ``data`` directory.
#: Secrets (.env, .dlt/secrets.toml) are excluded by default; opt in via
#: ``include_secrets=True`` in cloud.yml backup config or create_backup().
BACKUP_FILES = [
    ".dango/auth.db",
    ".dango/auth.yml",
    ".dango/dango.db",
    ".dango/scheduler.db",
    ".dango/project.yml",
    ".dango/sources.yml",
    ".dango/schedules.yml",
    ".dango/monitors.yml",
    ".dango/scripts.yml",
    ".dango/pii-overrides.yml",
    ".dango/cloud.yml",
    ".dango/logs/audit.jsonl",
    ".dango/logs/activity.jsonl",
    "dbt/profiles.yml",
    "dbt/dbt_project.yml",
    "dbt/packages.yml",
]

METABASE_CONFIG_FILE = ".dango/metabase.yml"

#: Directories to back up (recursively), relative to PROJECT_DIR.  Only regular
#: files and directories are copied; WAL and temporary files are left out
#: (``BACKUP_EXCLUDES``).
BACKUP_DIRS = [
    "data",
    "notebooks",
    "scripts",
    "dbt/models",
    "dbt/macros",
    "dbt/seeds",
    "custom_sources",
]

#: Databases in a backup.  Each is checkpointed before the copy, and a restore
#: clears its live WAL sidecars first (whichever list carries the file).
DATABASE_FILES = (
    "data/warehouse.duckdb",
    ".dango/auth.db",
    ".dango/dango.db",
    ".dango/scheduler.db",
)

#: Project paths deliberately left out of backups, with the reason.  Every path a
#: project can contain is either backed up or listed here (tests enforce it).
EXCLUDED_PATHS: dict[str, str] = {
    ".dango/backups": "backups themselves (would nest archives)",
    ".dango/cloud_key": "per-machine deploy credential",
    ".dango/credentials": "per-machine credentials",
    ".dango/secrets": "per-machine secrets",
    ".dango/ssh": "per-machine SSH keys",
    ".dango/known_hosts": "per-machine SSH host keys",
    ".dango/state": "runtime state (locks, deploy state), rebuilt on start",
    ".dango/history": "sync history, rebuilt by later syncs",
    ".dango/dbt_model_status.json": "runtime status, rebuilt by the next dbt run",
    ".dango/logs/scripts": "script run logs",
    ".dango/metabase_telemetry_state": "runtime telemetry state",
    ".dango/telemetry.json": "per-machine telemetry state",
    ".dango/routing.json": "local nginx routing",
    ".dango/web.pid": "runtime process id",
    ".dango/dev": "local dev-branch builds",
    ".dango/snapshots": "local notebook snapshots, recreated on demand",
    ".dlt/pipelines": "dlt load packages (copies of loaded rows); a restore moves the live copy aside so state is re-derived from the restored warehouse",
    ".dlt/config.toml": "generated by dango init",
    ".gitignore": "repository file, kept in git",
    ".pre-commit-config.yaml": "repository file, kept in git",
    "AGENTS.md": "generated by dango init",
    "CLAUDE.md": "generated by dango init",
    "COMPATIBILITY.md": "generated by dango init",
    "README.md": "generated by dango init",
    "SCALABILITY.md": "generated by dango init",
    "Dockerfile.metabase": "pushed by dango remote push",
    "docker-compose.yml": "regenerated from project.yml on start",
    "entrypoint.sh": "pushed by dango remote push",
    "requirements.txt": "pushed by dango remote push",
    "metabase": (
        "dashboard exports pushed by dango remote push; Metabase's own data is backed up "
        "from its volume under the archive's metabase/ directory"
    ),
    "metabase-plugins": "Metabase driver, pushed by dango remote push",
    "dbt/.user.yml": "dbt per-user id",
    "dbt/analyses": "kept in git (not synced to the server)",
    "dbt/tests": "kept in git (not synced to the server)",
    "dbt/snapshots": "kept in git (not synced to the server)",
    "dbt/dbt_packages": "installed by dbt deps",
    "dbt/logs": "dbt logs",
    "dbt/target": "dbt build output",
}

#: Secrets files excluded from backup by default.  Added dynamically when
#: ``include_secrets=True`` is passed to :func:`create_backup` or set in
#: the ``backup:`` section of cloud.yml.
SECRET_FILES = [
    ".dlt/secrets.toml",
    ".env",
]


def _sanitized_metabase_yaml_text(raw_config: str) -> str:
    """Return password-free Metabase metadata from YAML text.

    Backups intentionally retain URLs, database IDs, and other non-secret
    configuration, but never the Metabase administrator password.  Treat an
    invalid YAML document or non-mapping document as unsafe rather than
    guessing how to preserve it.
    """
    try:
        config = yaml.safe_load(raw_config)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid Metabase config: {exc}") from exc
    if not isinstance(config, dict):
        raise ValueError("invalid Metabase config: expected a YAML mapping")

    sanitized = dict(config)
    admin = sanitized.get("admin")
    if isinstance(admin, dict):
        sanitized_admin = dict(admin)
        sanitized_admin.pop("password", None)
        sanitized["admin"] = sanitized_admin
    return yaml.safe_dump(sanitized, sort_keys=False)


#: Upper bound for a Metabase metadata file; anything larger is not ours.
METABASE_CONFIG_MAX_BYTES = 1024 * 1024


def _read_config_nofollow(source: Path) -> str:
    """Read *source* without following a link at its name or in its directory.

    Root reads files in a tree the service user controls, so neither the file
    nor its parent directory may be a link, and the file must be a modest regular
    file.  Raises ``ValueError`` otherwise.
    """
    dir_fd = -1
    fd = -1
    try:
        dir_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fd = os.open(
            source.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
            dir_fd=dir_fd,
        )
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > METABASE_CONFIG_MAX_BYTES
        ):
            raise ValueError("invalid Metabase config: not a regular file of reasonable size")
        with os.fdopen(fd, encoding="utf-8") as handle:
            fd = -1
            return handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid Metabase config: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if dir_fd >= 0:
            os.close(dir_fd)


def _sanitized_metabase_yaml(source: Path) -> str:
    """Return password-free Metabase metadata read from *source* (links refused)."""
    return _sanitized_metabase_yaml_text(_read_config_nofollow(source))


def _write_sanitized_metabase_yaml(source: Path, destination: Path) -> None:
    """Write password-free Metabase metadata from *source* to *destination*."""
    sanitized = _sanitized_metabase_yaml(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sanitized)


def _sanitize_remote_metabase_yaml(ssh: SSHManager, source: str, destination: str) -> str | None:
    """Sanitize remote Metabase metadata, returning a non-secret warning on error."""
    script = """
from pathlib import Path
import os
import stat
import sys
import yaml

source, destination = map(Path, sys.argv[1:3])
try:
    dir_fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = os.open(
        source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY, dir_fd=dir_fd
    )
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1048576:
        raise ValueError("not a regular file of reasonable size")
    with os.fdopen(fd, encoding="utf-8") as handle:
        config = yaml.safe_load(handle.read())
    if not isinstance(config, dict):
        raise ValueError("expected a YAML mapping")
    sanitized = dict(config)
    admin = sanitized.get("admin")
    if isinstance(admin, dict):
        sanitized_admin = dict(admin)
        sanitized_admin.pop("password", None)
        sanitized["admin"] = sanitized_admin
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(yaml.safe_dump(sanitized, sort_keys=False))
except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
"""
    command = (
        f"{ROOT_PYTHON} -c {shlex.quote(script)} {shlex.quote(source)} {shlex.quote(destination)}"
    )
    result = ssh.exec_command(command, timeout=30)
    if result.success:
        return None
    _logger.warning("metabase_backup_metadata_omitted")
    return "Metabase metadata omitted because its YAML could not be safely sanitized"


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
    """Raise ``CloudProvisioningError`` if <*required_mb* MB free on /srv.

    Also logs a warning if disk usage exceeds 50%.
    """
    stdout = _run_checked(ssh, "df -m /srv/dango | tail -1", step="check_disk_space")
    parts = stdout.split()
    if len(parts) >= 5:
        try:
            available = int(parts[3])
        except ValueError:
            _logger.warning("disk_space_check_skipped", reason="unparseable df output")
            return
        if available < required_mb:
            raise CloudProvisioningError(
                f"Insufficient disk space: {available} MB available, "
                f"need at least {required_mb} MB for backup"
            )
        # Warn if disk usage exceeds 50% (column 5 is usage percentage)
        try:
            usage_pct_str = parts[4].rstrip("%")
            usage_pct = int(usage_pct_str)
            if usage_pct > 50:
                total_mb = int(parts[1]) if len(parts) >= 2 else 0
                _logger.warning(
                    "high_disk_usage",
                    usage_pct=usage_pct,
                    available_mb=available,
                    total_mb=total_mb,
                )
        except (ValueError, IndexError):  # noqa: BLE001
            # Best-effort: the usage warning is skipped if the df format is unexpected.
            _logger.warning("disk_usage_warning_skipped", reason="unparseable df output")


def helper_branch_active(ssh: SSHManager) -> bool:
    """True when the compose helper is installed and trusted (same test as the command wrapper)."""
    return ssh.exec_command(MARKER_TEST, timeout=15).success


def _stop_failure(code: int) -> ComposeStopError:
    if code == EXIT_BUSY:
        return DockerBusyError(DOCKER_BUSY_MESSAGE)
    return ComposeStopError(
        f"Metabase could not be stopped (compose helper exit {code}); the services were left running."
    )


def stop_services(ssh: SSHManager, on_first_stop: Callable[[], None] | None = None) -> None:
    """Stop dango-web and Metabase to ensure no concurrent writes.

    *on_first_stop* is called once the first Metabase stop has succeeded, before dango-web is
    stopped (``remote upgrade`` re-arms its guard there: each stop can wait for the helper lock).

    Best-effort — does not raise if services are already stopped.  With the compose
    helper, Metabase is stopped first (a busy or failed helper aborts with nothing
    stopped) and again after dango-web, which could have started it in between; any
    non-zero helper exit is a failure.
    """
    compose_project = get_remote_compose_project_name(ssh)
    compose_dir = f"{PROJECT_DIR}/docker-compose.yml"
    legacy_stop = (
        f"COMPOSE_PROJECT_NAME={compose_project} docker compose -f {compose_dir} "
        "stop metabase 2>/dev/null || true"
    )
    helper = helper_branch_active(ssh)
    first = ssh.exec_command(
        server_compose_cmd("stop", "metabase", legacy="true"),
        timeout=root_helper_timeout("stop") if helper else 60,
    )
    if first.exit_code != 0:
        if first.exit_code != EXIT_BUSY:
            start_services(ssh)  # a timed-out stop may have completed
        raise _stop_failure(first.exit_code)
    if on_first_stop is not None:
        on_first_stop()
    result = ssh.exec_command("systemctl stop dango-web", timeout=190)
    if result.exit_code != 0:
        _logger.warning("service_stop_failed", service="dango-web", stderr=result.stderr)
    result = ssh.exec_command(
        server_compose_cmd("stop", "metabase", legacy=legacy_stop),
        timeout=root_helper_timeout("stop") if helper else 120,
    )
    if result.exit_code != 0:
        if not helper:
            _logger.warning("service_stop_failed", service="metabase", stderr=result.stderr)
            return
        start_services(ssh)  # dango-web is already down: put everything back before aborting
        raise _stop_failure(result.exit_code)


def start_services(ssh: SSHManager) -> str | None:
    """Start Metabase then dango-web (reverse order of stop).

    With the compose helper dango-web is started first, whatever the helper does: its own
    start brings Metabase up, so a busy or failing helper never keeps the service down.
    Returns a message when Metabase was not started here.
    """
    compose_project = get_remote_compose_project_name(ssh)
    legacy_start = (
        f"COMPOSE_PROJECT_NAME={compose_project} docker compose -f {PROJECT_DIR}/docker-compose.yml "
        "start metabase 2>/dev/null || true"
    )
    helper = helper_branch_active(ssh)

    def _web() -> None:
        result = ssh.exec_command("systemctl start dango-web", timeout=60)
        if result.exit_code != 0:
            _logger.warning("service_start_failed", service="dango-web", stderr=result.stderr)

    if helper:
        _web()
    result = ssh.exec_command(
        server_compose_cmd("start", "metabase", legacy=legacy_start),
        timeout=root_helper_timeout("start") if helper else 120,
    )
    message: str | None = None
    if result.exit_code != 0:
        _logger.warning("service_start_failed", service="metabase", stderr=result.stderr)
        if helper:
            reason = (
                "another Docker operation was in progress"
                if result.exit_code == EXIT_BUSY
                else (f"the compose helper exited {result.exit_code}")
            )
            message = (
                f"Metabase was not started: {reason}. "
                "Run `dango-compose start metabase` on the server once it is free."
            )
    if not helper:
        _web()
    return message


def _checkpoint_duckdb(ssh: SSHManager) -> bool:
    """Run CHECKPOINT on DuckDB as the service user (it owns the file).  False if missing."""
    db_path = f"{PROJECT_DIR}/data/warehouse.duckdb"
    if not ssh.exec_command(f"test -f {db_path}").success:
        return False
    _run_checked(
        ssh,
        f"{SERVICE_PYTHON} -c \"import duckdb; c=duckdb.connect('{db_path}'); "
        f"c.execute('CHECKPOINT'); c.close()\"",
        step="checkpoint_duckdb",
        timeout=120,
    )
    return True


def _checkpoint_auth_db(ssh: SSHManager) -> bool:
    """Run WAL checkpoint on auth.db as the service user (it owns the file).  False if missing."""
    db_path = f"{PROJECT_DIR}/.dango/auth.db"
    if not ssh.exec_command(f"test -f {db_path}").success:
        return False
    _run_checked(
        ssh,
        f"{SERVICE_PYTHON} -c \"import sqlite3; c=sqlite3.connect('{db_path}'); "
        f"c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()\"",
        step="checkpoint_auth_db",
        timeout=30,
    )
    return True


def _checkpoint_sqlite(ssh: SSHManager, rel_path: str) -> str | None:
    """Fold the WAL of SQLite database *rel_path* into its file; returns a warning on failure.

    Used for the databases other than auth.db (which has its own step); a missing
    file needs no checkpoint and returns ``None``.
    """
    db_path = f"{PROJECT_DIR}/{rel_path}"
    if not ssh.exec_command(f"test -f {shlex.quote(db_path)}").success:
        return None
    script = (
        "import sqlite3, sys; c=sqlite3.connect(sys.argv[1]);"
        " c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
    )
    # As the service user: root opening the database could leave root-owned WAL files.
    result = ssh.exec_command(
        f"{SERVICE_PYTHON} -c {shlex.quote(script)} {shlex.quote(db_path)}",
        timeout=30,
    )
    if result.success:
        return None
    _logger.warning("checkpoint_failed", database=rel_path)
    return f"Could not checkpoint {rel_path}; its most recent writes may be missing from the backup"


def _get_metabase_volume_path(ssh: SSHManager) -> str | None:
    """Discover the host path of the project's Metabase H2 Docker volume."""
    compose_project = get_remote_compose_project_name(ssh)
    legacy = (
        f"docker volume inspect {compose_project}_metabase-data "
        "--format '{{.Mountpoint}}' 2>/dev/null"
    )
    command = server_compose_cmd("volume-path", legacy=legacy)
    if helper_branch_active(ssh):
        result = ssh.exec_command(command, timeout=root_helper_timeout("volume-path"))
    else:
        result = ssh.exec_command(command)
    return result.stdout.strip() if result.success and result.stdout.strip() else None


def _create_archive(
    ssh: SSHManager,
    timestamp: str,
    metabase_vol_path: str | None,
    backup_type: str,
    *,
    git_commit: str | None = None,
    git_branch: str | None = None,
    include_secrets: bool = False,
    warnings: list[str] | None = None,
) -> tuple[str, BackupManifest]:
    """Create a tar.gz archive in BACKUP_DIR.  Returns (archive_path, manifest).

    Root stages the copy in a fresh private directory and copies only regular files
    and directories: links and special files in the project are skipped with a
    warning, never followed.  The archive and manifest are created 0600 and never
    through an existing path; archive members keep their source modes.
    """
    archive_name = f"backup-{timestamp}"
    archive_path = f"{BACKUP_DIR}/{archive_name}.tar.gz"
    manifest_dest = f"{BACKUP_DIR}/{archive_name}.json"
    partial_path = f"{archive_path}.partial"
    private = ""
    manifest_written = committed = False

    try:
        _run_checked(ssh, f"mkdir -p {shlex.quote(BACKUP_DIR)}", step="create_staging")
        made = _run_checked(
            ssh,
            f"mktemp -d {shlex.quote(BACKUP_STAGING_ROOT + '/dango-backup.XXXXXXXX')}",
            step="create_staging",
        ).strip()
        if not made.startswith(BACKUP_STAGING_ROOT.rstrip("/") + "/dango-backup."):
            raise CloudProvisioningError(
                "Backup step 'create_staging' failed: no private directory"
            )
        private = made
        staging = f"{private}/{archive_name}"
        q_staging = shlex.quote(staging)
        _run_checked(ssh, f"mkdir {q_staging}", step="create_staging")

        # Build runtime file list — secrets excluded by default
        file_list = list(BACKUP_FILES)
        if include_secrets:
            file_list.extend(SECRET_FILES)

        # Sources absent on the server (e.g. no notebooks yet) are skipped; a failed
        # copy fails the step.  ``cp`` without ``-p``: no set-id bits, root owns copies.
        copy_cmds: list[str] = []
        for fpath in file_list:
            dest_dir = f"{staging}/{fpath.rsplit('/', 1)[0]}" if "/" in fpath else staging
            copy_cmds.append(backup_file_cmd(f"{PROJECT_DIR}/{fpath}", dest_dir, fpath))
        for dpath in BACKUP_DIRS:
            copy_cmds.append(backup_dir_cmd(f"{PROJECT_DIR}/{dpath}", f"{staging}/{dpath}", dpath))
        metabase_source = f"{PROJECT_DIR}/{METABASE_CONFIG_FILE}"
        metabase_destination = f"{staging}/{METABASE_CONFIG_FILE}"
        if ssh.exec_command(f"test -f {shlex.quote(metabase_source)}").success:
            warning = _sanitize_remote_metabase_yaml(ssh, metabase_source, metabase_destination)
            if warning and warnings is not None:
                warnings.append(warning)
        if metabase_vol_path:
            for h2 in ("metabase.db.mv.db", "metabase.db.trace.db"):
                copy_cmds.append(copy_file_cmd(f"{metabase_vol_path}/{h2}", f"{staging}/metabase"))
        copied = _run_checked(ssh, " && ".join(copy_cmds), step="copy_files", timeout=300)
        pruned = _run_checked(ssh, prune_backup_staging_cmd(staging), step="copy_files")
        for rel in skipped_from_output(copied) + skipped_from_output(pruned):
            _logger.warning("backup_entry_skipped", path=rel)
            if warnings is not None:
                warnings.append(skipped_warning(rel))

        # Get version + file info for manifest
        ver = ssh.exec_command(f'{ROOT_PYTHON} -c "import dango; print(dango.__version__)"')
        dango_version = ver.stdout.strip() if ver.success else "unknown"

        fi = ssh.exec_command(f"find {q_staging} -type f -exec stat --format='%n %s' {{}} \\;")
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
            f"cat > {shlex.quote(staging + '/manifest.json')} << 'MANIFEST_EOF'\n"
            f"{manifest_json}\nMANIFEST_EOF",
            step="write_manifest",
        )
        # New 0600 files only (noclobber): never written through a planted path.
        # Written under a name the backup listing ignores, then renamed: an interrupted
        # tar never leaves a truncated ``*.tar.gz`` that retention or rollback would pick.
        _run_checked(
            ssh,
            f"(set -C; umask 077; tar -czf - -C {shlex.quote(private)} {archive_name}"
            f" > {shlex.quote(partial_path)})",
            step="create_archive",
            timeout=600,
        )
        # The manifest goes in before the archive appears: the archive is the commit marker.
        _run_checked(
            ssh,
            f"(set -C; umask 077; cat {shlex.quote(staging + '/manifest.json')}"
            f" > {shlex.quote(manifest_dest)})",
            step="copy_manifest",
        )
        manifest_written = True
        # ``-n``: never over an existing path (a planted link included); old coreutils skip
        # silently, so the partial file still being there means the rename did not happen.
        _run_checked(
            ssh,
            f"mv -T -n {shlex.quote(partial_path)} {shlex.quote(archive_path)}"
            f" && ! [ -e {shlex.quote(partial_path)} ]",
            step="create_archive",
        )
        committed = True
    finally:
        _remove_leftovers(ssh, [p for p in (private, partial_path) if p])
        if manifest_written and not committed:
            # The rename may have happened just before an interruption: only drop a
            # manifest whose archive does not exist.
            with contextlib.suppress(Exception):
                ssh.exec_command(
                    f"[ -e {shlex.quote(archive_path)} ] || rm -f {shlex.quote(manifest_dest)}"
                )

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
    on_server_retention: int = 1,
    include_secrets: bool = False,
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
        on_server_retention: Number of on-server backups to keep (default 1).
        include_secrets: If True, include .env and .dlt/secrets.toml in backup.
    """
    start_time = time.monotonic()
    warnings: list[str] = []
    timestamp = time.strftime("%Y%m%d-%H%M%S")

    _sweep_stale_leftovers(ssh)  # first: leftovers of a dead run may be what filled the disk
    _notify(on_progress, "check_disk_space", "running")
    _check_disk_space(ssh)
    _notify(on_progress, "check_disk_space", "done")

    nothing_stopped = False
    start_warning: str | None = None
    try:
        # Inside the try: an interruption after the stop must still restart the services.
        _notify(on_progress, "stop_services", "running")
        try:
            stop_services(ssh)
        except ComposeStopError:
            nothing_stopped = True  # the services are still (or again) running
            raise
        _notify(on_progress, "stop_services", "done")

        _notify(on_progress, "checkpoint_databases", "running")
        if not _checkpoint_duckdb(ssh):
            warnings.append("DuckDB warehouse not found — skipped checkpoint")
        if not _checkpoint_auth_db(ssh):
            warnings.append("Auth database not found — skipped checkpoint")
        for rel_path in (".dango/dango.db", ".dango/scheduler.db"):
            if warning := _checkpoint_sqlite(ssh, rel_path):
                warnings.append(warning)
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
            include_secrets=include_secrets,
            warnings=warnings,
        )
        _notify(on_progress, "create_archive", "done")
    finally:
        if restart_services and not nothing_stopped:
            try:
                _notify(on_progress, "start_services", "running")
            finally:
                start_warning = start_services(ssh)
            _notify(on_progress, "start_services", "done")
    if start_warning:
        warnings.append(start_warning)

    _notify(on_progress, "rotate_backups", "running")
    rotate_local_backups(ssh, keep=on_server_retention)
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
        size_result = ssh.exec_command(f"stat --format='%s' {shlex.quote(path)} 2>/dev/null")
        try:
            size = int(size_result.stdout.strip()) if size_result.success else 0
        except ValueError:
            size = 0
        date = name[7:22] if name.startswith("backup-") and len(name) >= 22 else ""
        backups.append({"name": name, "path": path, "size_bytes": size, "date": date})
    return backups


def _is_safety_backup(ssh: SSHManager, archive_path: str) -> bool:
    """True if the archive's manifest says it is a pre-restore safety backup."""
    manifest = ssh.exec_command(
        f"cat {shlex.quote(archive_path.replace('.tar.gz', '.json'))} 2>/dev/null"
    )
    if not manifest.success:
        return False
    try:
        return bool(json.loads(manifest.stdout).get("backup_type") == "pre-restore")
    except (ValueError, AttributeError):
        return False


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
        # The newest archive after a restore is its own pre-restore safety backup, which
        # holds the state the restore replaced: never the default to roll back to.
        chosen = next(
            (b for b in list_local_backups(ssh) if not _is_safety_backup(ssh, b["path"])), None
        )
        if chosen is None:
            raise CloudProvisioningError("No backups found to restore from")
        backup_path = chosen["path"]
    else:
        if not ssh.exec_command(f"test -f {shlex.quote(backup_path)}").success:
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

    if not ssh.exec_command(f"test ! -L {shlex.quote(PROJECT_DIR)}").success:
        raise CloudProvisioningError(
            f"{PROJECT_DIR} is a symlink; refusing to restore into it: nothing was restored"
        )

    manifest_path = archive_path.replace(".tar.gz", ".json")
    manifest_result = ssh.exec_command(f"cat {shlex.quote(manifest_path)} 2>/dev/null")
    if manifest_result.success and manifest_result.stdout.strip():
        _notify(on_progress, "read_manifest", "done")

    # Extract and check the archive before anything else, so a refused archive
    # leaves the server untouched (no safety backup, services still running).
    # Generated, unique staging dir: independent of the archive's file name
    # and of the top-level directory name inside the archive.
    staging = make_private_dir(ssh, "dango-restore-staging", service_access=True)
    _notify(on_progress, "extract_archive", "running")
    extract_checked_archive(
        ssh,
        archive_path,
        staging,
        [METABASE_CONFIG_FILE, *BACKUP_FILES, *BACKUP_DIRS, "metabase"],
    )
    try:
        _notify(on_progress, "extract_archive", "done")

        # Pre-restore safety backup — covers all three restore paths
        # (rollback(), _backup_restore_from_local(), and migrate.py all flow
        # through restore_from_archive).  on_server_retention=999 is an
        # intentionally large value to prevent rotation from deleting this
        # safety backup before the restore completes.
        _notify(on_progress, "pre_restore_backup", "running")
        try:
            safety = create_backup(
                ssh,
                backup_type="pre-restore",
                restart_services=False,
                on_server_retention=999,
            )
            warnings.append(f"Pre-restore safety backup created: {safety.archive_path}")
            safety_ok = True
        except Exception as exc:
            safety_ok = False
            warnings.append(f"Pre-restore safety backup skipped (continuing): {exc}")
            _logger.warning("pre_restore_backup_failed", error=str(exc))
        _notify(on_progress, "pre_restore_backup", "done")

        _notify(on_progress, "stop_services", "running")
        stop_services(ssh)
        _notify(on_progress, "stop_services", "done")
    except ComposeStopError:
        # stop_services left the services running (or put them back): a second start would wait on
        # the helper lock again for nothing.
        _remove_leftovers(ssh, [staging])
        raise
    except BaseException:
        # The safety backup stopped the services and the extracted archive holds project
        # data: put the services back and drop the copy before the interruption propagates.
        with contextlib.suppress(Exception):
            start_services(ssh)
        _remove_leftovers(ssh, [staging])
        raise

    services_restarted = False
    copies_started = False
    private_dir = ""
    moved_aside: list[str] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        try:
            _notify(on_progress, "restore_files", "running")
            # Sanitize Metabase metadata as root into a fresh private directory
            # (never into staging or the project tree), then let the service user read it.
            metabase_source = f"{staging}/{METABASE_CONFIG_FILE}"
            if ssh.exec_command(f"test -f {shlex.quote(metabase_source)}").success:
                try:
                    private_dir = make_private_dir(ssh, "dango-restore-meta", service_access=True)
                except CloudProvisioningError:
                    private_dir = ""
                if not private_dir:
                    warnings.append("Metabase metadata omitted: no private temporary directory")
                else:
                    sanitized = f"{private_dir}/{SANITIZED_METABASE_NAME}"
                    warning = _sanitize_remote_metabase_yaml(ssh, metabase_source, sanitized)
                    if warning:
                        warnings.append(warning)
                    q_file = shlex.quote(sanitized)
                    _run_checked(
                        ssh,
                        f"if [ -f {q_file} ]; then chown {SERVICE_USER}:{SERVICE_USER} {q_file}; fi",
                        step="restore_files",
                    )
            # Project files are written as the service user, which owns the project tree.
            copies_started = True
            _run_checked(ssh, hand_staging_to_service_user_cmd(staging), step="restore_files")
            restore_cmds: list[str] = []
            for db_path in DATABASE_FILES:
                # Clear the live database's leftover WAL first, or it is replayed into
                # the restored file on the next open.  Without a safety backup the WAL
                # may hold the only copy of recent writes, so it is moved aside instead.
                clear: list[str] = []
                for side in (f"{PROJECT_DIR}/{s}" for s in wal_sidecar_names(db_path)):
                    q = shlex.quote(side)
                    if safety_ok:
                        clear.append(f"{AS_SERVICE_USER} rm -f {q}")
                    else:
                        kept = shlex.quote(f"{side}.pre-restore-{stamp}")
                        clear.append(f"if [ -e {q} ]; then {AS_SERVICE_USER} mv -f {q} {kept}; fi")
                        moved_aside.append(f"{side}.pre-restore-{stamp}")
                restore_cmds.append(
                    f"if [ -f {shlex.quote(f'{staging}/{db_path}')} ]; then {' && '.join(clear)}; fi"
                )
            restore_cmds.append(set_aside_pipeline_state_cmd(staging, PROJECT_DIR, stamp))
            auth_warning = _auth_config_refusal(ssh, staging)
            skip_auth_config = auth_warning is not None
            if auth_warning:
                warnings.append(auth_warning)
            for fpath in BACKUP_FILES:
                if fpath == AUTH_CONFIG_FILE and skip_auth_config:
                    continue
                restore_cmds.append(
                    restore_file_cmd(f"{staging}/{fpath}", f"{PROJECT_DIR}/{fpath}")
                )
            if private_dir:
                restore_cmds.append(
                    restore_file_cmd(
                        f"{private_dir}/{SANITIZED_METABASE_NAME}",
                        f"{PROJECT_DIR}/{METABASE_CONFIG_FILE}",
                    )
                )
            for dpath in BACKUP_DIRS:
                restore_cmds.append(restore_backup_dir_cmd(staging, PROJECT_DIR, dpath))
            _run_checked(ssh, " && ".join(restore_cmds), step="restore_files", timeout=300)
            for kept_path in moved_aside:
                if ssh.exec_command(f"test -e {shlex.quote(kept_path)}").success:
                    warnings.append(f"Live database WAL kept (no safety backup): {kept_path}")
            _notify(on_progress, "restore_files", "done")

            _notify(on_progress, "restore_metabase", "running")
            metabase_vol = _get_metabase_volume_path(ssh)
            if metabase_vol:
                h2_cmds = [
                    restore_volume_file_cmd(f"{staging}/metabase/{h2}", metabase_vol, h2)
                    for h2 in ("metabase.db.mv.db", "metabase.db.trace.db")
                ]
                h2_cmds.append(clear_metabase_lock_state_cmd(PROJECT_DIR))
                _run_checked(ssh, " && ".join(h2_cmds), step="restore_metabase", timeout=300)
            else:
                warnings.append("Metabase Docker volume not found — H2 restore skipped")
            _notify(on_progress, "restore_metabase", "done")

            _notify(on_progress, "fix_ownership", "running")
            _run_checked(ssh, "chown -R dango:dango /srv/dango/project", step="fix_ownership")
            # the restore can recreate protected paths without their denials (services are stopped)
            from dango.platform.cloud import runner_reapply

            runner_reapply.reapply(ssh, warnings)
            _notify(on_progress, "fix_ownership", "done")
        except BaseException:
            if copies_started:
                # A failed copy leaves a partial, root-owned restore; give it back
                # to the service user before services restart on it.
                with contextlib.suppress(Exception):  # best effort; keep the original error
                    ssh.exec_command("chown -R dango:dango /srv/dango/project")
                # notebooks/ may already be recreated without its ACL; put it back before services start
                from dango.platform.cloud import runner_reapply

                with contextlib.suppress(Exception):
                    runner_reapply.reapply(ssh, warnings)
            raise
        finally:
            _remove_leftovers(ssh, [p for p in (staging, private_dir) if p])
    finally:
        try:
            _notify(on_progress, "start_services", "running")
        finally:
            try:
                # A server set up fresh (migrate) has the helper but no pinned project yet.
                from dango.platform.cloud.compose_migrate import ensure_pinned

                ensure_pinned(ssh, warnings)
            except Exception as exc:
                warnings.append(f"The compose helper could not be prepared: {exc}")
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


def make_private_dir(ssh: SSHManager, prefix: str, *, service_access: bool = False) -> str:
    """Create a fresh, uniquely named directory under :data:`RESTORE_STAGING_ROOT`; return its path.

    Root-only (``0700``), or ``root:dango 0750`` with *service_access* so the service user can
    read what root hands it there.  Removed again if it could not be fully set up.
    """
    path = f"{RESTORE_STAGING_ROOT}/{prefix}-{uuid.uuid4().hex}"
    result = ssh.exec_command(private_dir_cmd(path, service_access=service_access))
    if not result.success:
        _remove_leftovers(ssh, [path])
        raise CloudProvisioningError(
            f"Could not create a private directory for the restore: "
            f"{result.stderr.strip() or f'exit {result.exit_code}'}"
        )
    return path


def _remove_leftovers(ssh: SSHManager, paths: list[str]) -> None:
    """Best-effort ``rm -rf`` of temporary directories; a failure is logged, never raised.

    The directories may hold copies of project data (and secrets), so a leftover is named.
    """
    command = "rm -rf " + " ".join(shlex.quote(p) for p in paths)
    try:
        result = ssh.exec_command(command)
        failed = not result.success
    except Exception:  # cleanup must never mask the operation's own outcome
        failed = True
    if failed:
        _logger.warning("temporary_directory_not_removed", paths=paths)


def _auth_config_refusal(ssh: SSHManager, staging: str) -> str | None:
    """Warning if the staged ``auth.yml`` must not be restored, else ``None``.

    Refused when it is larger than :data:`AUTH_CONFIG_MAX_BYTES` (too large to check in
    full) or does not say ``enabled: true``; an absent file needs no warning.
    """
    staged = shlex.quote(f"{staging}/{AUTH_CONFIG_FILE}")
    if not ssh.exec_command(f"test -f {staged}").success:
        return None
    size = ssh.exec_command(f"wc -c < {staged}")
    if not size.success or not size.stdout.strip().isdigit():
        return AUTH_CONFIG_SKIPPED_WARNING
    if int(size.stdout.strip()) > AUTH_CONFIG_MAX_BYTES:
        return AUTH_CONFIG_OVERSIZED_WARNING
    result = ssh.exec_command(f"cat {staged}")
    if result.success and auth_config_keeps_auth_on(result.stdout):
        return None
    return AUTH_CONFIG_SKIPPED_WARNING


#: Minutes after which a ``*.partial`` archive or orphaned manifest is a dead run's leftover
#: (longer than the slowest ``tar``, 600 s).
_STALE_FILE_MINUTES = 15
#: Staging directories: a directory's mtime only changes when children are added or removed,
#: so a live tar or restore can look old, and restore/rollback/resize/migrate take no deploy
#: lock. Far above any operation, including the scheduled backup's TimeoutStartSec=900.
_STALE_STAGING_MINUTES = 360


def _sweep_stale_leftovers(ssh: SSHManager) -> None:
    """Remove what a backup or restore whose client died could not clean up.

    ``find`` never follows links; only root-owned entries directly in ``/tmp``, the private restore directory and the
    backup directory are touched.
    """
    names = " -o ".join(
        f"-name {shlex.quote(n)}"
        for n in (
            "dango-backup.*",
            "dango-restore-staging-*",
            "dango-restore-upload-*",
            "dango-restore-meta-*",
        )
    )
    backup_dir = shlex.quote(BACKUP_DIR)
    orphan_manifests = (
        'for f; do [ -e "${f%.json}.tar.gz" ] || rm -f "$f"; done'  # no sibling archive
    )
    for command in (
        f"find {backup_dir} -maxdepth 1 -type f"
        f" -name {shlex.quote('backup-*.tar.gz.partial')} -mmin +{_STALE_FILE_MINUTES} -delete",
        f"find {backup_dir} -maxdepth 1 -type f -name {shlex.quote('backup-*.json')}"
        f" -mmin +{_STALE_FILE_MINUTES} -exec sh -c {shlex.quote(orphan_manifests)} sh {{}} +",
        f"find /tmp {shlex.quote(RESTORE_STAGING_ROOT)} -maxdepth 1 -type d -user root"
        f" \\( {names} \\)"
        f" -mmin +{_STALE_STAGING_MINUTES} -exec rm -rf {{}} +",
    ):
        try:
            ssh.exec_command(command)
        except Exception:
            _logger.warning("stale_leftover_sweep_failed", exc_info=True)


def rotate_local_backups(ssh: SSHManager, keep: int = MAX_LOCAL_BACKUPS) -> int:
    """Delete old backup archives beyond the retention limit."""
    result = ssh.exec_command(f"ls -1t {BACKUP_DIR}/*.tar.gz 2>/dev/null || true")
    if not result.success or not result.stdout.strip():
        return 0
    archives = [line.strip() for line in result.stdout.strip().splitlines() if line.strip()]
    if len(archives) <= keep:
        return 0
    to_delete = archives[keep:]
    if to_delete and len(to_delete) == len(archives):
        _logger.warning("skip_rotate_last_backup", keep=keep, archive_count=len(archives))
        return 0
    deleted = 0
    for archive in to_delete:
        ssh.exec_command(
            f"rm -f {shlex.quote(archive)} {shlex.quote(archive.replace('.tar.gz', '.json'))}"
        )  # cleanup: silent OK
        deleted += 1
    return deleted
