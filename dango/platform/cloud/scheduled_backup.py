"""dango/platform/cloud/scheduled_backup.py

Server-side scheduled backup for Dango cloud deployments.  Runs ON the
remote server (not via SSH from local).  Entry point::

    python -m dango.platform.cloud.scheduled_backup
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger
from dango.platform.cloud.compose_helper import COMPOSE_DIR, HELPER_PATH, SRV_DIR
from dango.platform.compose_exec import EXIT_BUSY, uses_helper
from dango.platform.docker import get_compose_project_name
from dango.utils.env_file import parse_env_file

_logger = get_logger(__name__)

PROJECT_DIR = Path("/srv/dango/project")
BACKUP_DIR = Path("/srv/dango/backups/deploy")
HEALTH_FILE = Path("/srv/dango/backups/.backup_health.json")
LOCK_FILE = Path("/srv/dango/backups/.backup.lock")
MARKER_FILE = Path("/srv/dango/backups/.dango-backup-initialized")
VENV_PYTHON = "/srv/dango/venv/bin/python"
SPACES_PREFIX = "backups/"
SPACES_KEY_NAMES = ("SPACES_ACCESS_KEY", "SPACES_SECRET_KEY")
#: Upper bound for a project ``.env`` read by the root backup process.
ENV_FILE_MAX_BYTES = 1024 * 1024
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


def _run_local(args: list[str], *, step: str, timeout: int = 120) -> str:
    """Run a command (argument list, no shell) locally, raising on failure. Returns stdout."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CloudProvisioningError(f"Step '{step}' timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise CloudProvisioningError(
            f"Step '{step}' failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _run_builder_script(script: str, *, step: str, timeout: int = 120) -> str:
    """Run a script built by :mod:`backup_restore` under ``sh -c``, raising on failure.

    Those builders are shared with the SSH restore path, which needs one string, and
    quote every interpolated value with :func:`shlex.quote`. Only the script text is
    interpreted by the shell; nothing is passed with ``shell=True``.
    """
    # Deliberate: the builders also feed the SSH restore path (one string per command);
    # every value in *script* is shlex-quoted or a constant, so this is not injectable.
    return _run_local(["sh", "-c", script], step=step, timeout=timeout)


def _run_best_effort(args: list[str], *, timeout: int, env: dict[str, str] | None = None) -> None:
    """Run *args* tolerating failure: a non-zero exit or missing program is logged, not raised."""
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            _logger.warning(
                "backup_service_command_failed", program=args[0], returncode=result.returncode
            )
    except OSError:
        _logger.warning("backup_service_command_unavailable", program=args[0])


def _compose_env() -> dict[str, str]:
    """Environment for ``docker compose`` with this project's compose project name."""
    return {**os.environ, "COMPOSE_PROJECT_NAME": get_compose_project_name(PROJECT_DIR)}


def _metabase_compose(action: str) -> None:
    """Run ``docker compose <action> metabase`` for the project (best effort)."""
    _run_best_effort(
        ["docker", "compose", "-f", f"{PROJECT_DIR}/docker-compose.yml", action, "metabase"],
        timeout=120,
        env=_compose_env(),
    )


#: Shown when the compose helper reports that another Docker operation holds its lock.
DOCKER_BUSY_MESSAGE = (
    "Another Docker operation is in progress on the server; the backup was not started "
    "and will be retried on the next run."
)


#: A helper call may take at most this long: the timer's unit has 900 s in all, and a
#: stop, a start and the dango-web commands must fit inside it (and in ``TimeoutStopSec``).
_HELPER_CALL_LIMIT = 120
#: Grace after SIGTERM (the helper forwards it to Docker) before the call is killed.
_HELPER_TERM_GRACE = 15
#: The helper is used only when these are root-owned and not group/other-writable too.
_HELPER_PARENT_DIRS = (SRV_DIR, COMPOSE_DIR)
_TRUSTED_UID = 0


def _helper_enabled() -> bool:
    """The trusted marker exists and its parent directories are root-owned and not writable by others."""
    if not uses_helper():
        return False
    try:
        for path in _HELPER_PARENT_DIRS:
            info = os.lstat(path)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != _TRUSTED_UID
                or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                return False
    except OSError:
        return False
    return True


def _helper_call(verb: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a compose-helper verb as root for at most ``_HELPER_CALL_LIMIT`` seconds.

    On timeout the helper gets SIGTERM (it stops its Docker child), then SIGKILL, and
    ``TimeoutExpired`` is raised: a call that gave up never leaves a queued helper behind.
    """
    argv = [HELPER_PATH, verb, *args]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out, err = proc.communicate(timeout=_HELPER_CALL_LIMIT)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=_HELPER_TERM_GRACE)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _helper_stop_metabase() -> None:
    """Stop Metabase through the helper; busy, timeout or any non-zero exit aborts.

    A busy helper means nothing was stopped; for any other failure (a timed-out stop may
    have completed) the caller's restart path must run, so the error says so.
    """
    try:
        result = _helper_call("stop", "metabase")
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _HelperStopFailed(f"{DOCKER_BUSY_MESSAGE} (stop timed out)", restart=True) from exc
    if result.returncode == EXIT_BUSY:
        raise _HelperStopFailed(DOCKER_BUSY_MESSAGE, restart=False)
    if result.returncode != 0:
        raise _HelperStopFailed(
            f"Metabase could not be stopped (compose helper exit {result.returncode}).",
            restart=True,
        )


class _HelperStopFailed(CloudProvisioningError):
    """A helper stop failed; ``restart`` says whether the restart path must run."""

    def __init__(self, message: str, *, restart: bool) -> None:
        super().__init__(message)
        self.restart = restart


def _stop_services() -> None:
    """Stop dango-web and Metabase.

    Best-effort — does not raise if services are already stopped.  With the compose
    helper, Metabase is stopped first (a busy or failed helper aborts with nothing
    stopped) and again after dango-web, which could have started it in between.
    """
    if not _helper_enabled():
        _run_best_effort(["systemctl", "stop", "dango-web"], timeout=60)
        _metabase_compose("stop")
        return
    try:
        _helper_stop_metabase()
    except _HelperStopFailed as exc:
        if exc.restart:
            _start_services()
        raise
    _run_best_effort(["systemctl", "stop", "dango-web"], timeout=60)
    try:
        _helper_stop_metabase()
    except _HelperStopFailed:
        _start_services()  # dango-web is already down: put everything back before aborting
        raise


def _start_services() -> None:
    """Start Metabase then dango-web.

    With the helper dango-web is started first, whatever the helper does: its own start
    brings Metabase up, so a busy, hung or failing helper never keeps the service down.
    """
    if not _helper_enabled():
        _metabase_compose("start")
        _run_best_effort(["systemctl", "start", "dango-web"], timeout=60)
        return
    _run_best_effort(["systemctl", "start", "dango-web"], timeout=60)
    try:
        result = _helper_call("start", "metabase")
        failed = result.returncode
    except (subprocess.TimeoutExpired, OSError):
        failed = -1
    if failed != 0:
        _logger.error(
            "service_start_failed",
            service="metabase",
            returncode=failed,
            hint="run `dango-compose start metabase` once the other operation finishes",
        )


_CHECKPOINT_SCRIPTS = {
    "DuckDB": "import duckdb, sys; c = duckdb.connect(sys.argv[1]); c.execute('CHECKPOINT'); c.close()",
    "Auth DB": (
        "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); "
        "c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
    ),
}


#: ``*.wal`` files are not archived, so a failed checkpoint leaves recent writes out.
_WRITES_MAY_BE_MISSING = "; its most recent writes may be missing from the backup"


def _checkpoint_as_service_user(label: str, db_path: Path) -> str | None:
    """Checkpoint *db_path* as ``dango`` (it owns the file; root would leave root-owned
    ``-wal``/``-shm`` files). Runs the venv interpreter isolated, from ``/``.
    Returns a warning on failure."""
    argv = [sys.executable, "-I", "-c"]
    if os.geteuid() == 0:
        from dango.platform.cloud.backup_restore import SERVICE_USER

        argv = ["runuser", "-u", SERVICE_USER, "--", VENV_PYTHON, "-I", "-c"]
    try:
        result = subprocess.run(
            [*argv, _CHECKPOINT_SCRIPTS[label], str(db_path)],
            cwd="/",
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{label} checkpoint failed: {exc}{_WRITES_MAY_BE_MISSING}"
    if result.returncode != 0:
        return f"{label} checkpoint failed: {result.stderr.strip()[-300:]}{_WRITES_MAY_BE_MISSING}"
    return None


def _checkpoint_databases() -> list[str]:
    """Run CHECKPOINT on DuckDB and WAL checkpoint on auth.db, as dango. Returns warnings."""
    warnings: list[str] = []
    for label, db_path in (
        ("DuckDB", PROJECT_DIR / "data" / "warehouse.duckdb"),
        ("Auth DB", PROJECT_DIR / ".dango" / "auth.db"),
    ):
        if not db_path.exists():
            name = "DuckDB warehouse" if label == "DuckDB" else "Auth database"
            warnings.append(f"{name} not found — skipped checkpoint")
            continue
        warning = _checkpoint_as_service_user(label, db_path)
        if warning:
            warnings.append(warning)
    for rel_path in (".dango/dango.db", ".dango/scheduler.db"):
        if warning := _checkpoint_sqlite(PROJECT_DIR / rel_path):
            warnings.append(warning)
    return warnings


def _checkpoint_sqlite(db_path: Path) -> str | None:
    """Fold the WAL of the SQLite database at *db_path* into its file; returns a warning on failure.

    Used for the databases other than auth.db; a missing file needs no checkpoint.
    """
    if not db_path.is_file() or db_path.is_symlink():
        return None
    script = (
        "import sqlite3, sys; c=sqlite3.connect(sys.argv[1]);"
        " c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()"
    )
    command = [sys.executable, "-I", "-c", script, str(db_path)]
    if os.geteuid() == 0:
        # As the service user: root opening the database could leave root-owned WAL files.
        from dango.platform.cloud.backup_restore import SERVICE_USER

        command = ["runuser", "-u", SERVICE_USER, "--", *command]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=60, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{db_path.name} checkpoint failed: {exc}"
    if result.returncode != 0:
        return f"{db_path.name} checkpoint failed: {result.stderr.strip()[-200:]}"
    return None


def _get_metabase_volume_path() -> str | None:
    """Discover the host path of the Metabase H2 Docker volume."""
    try:
        if _helper_enabled():
            result = _helper_call("volume-path")
        else:
            compose_project = get_compose_project_name(PROJECT_DIR)
            result = subprocess.run(
                [
                    "docker",
                    "volume",
                    "inspect",
                    f"{compose_project}_metabase-data",
                    "--format",
                    "{{.Mountpoint}}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        _logger.warning("metabase_volume_lookup_failed")
    return None


def read_env_values(
    path: Path | str, names: tuple[str, ...], max_bytes: int = ENV_FILE_MAX_BYTES
) -> dict[str, str]:
    """Return only *names* from the ``.env`` file at *path*, read as data.

    The file is written by the ``dango`` user and read by root, so it is never
    exported into the process environment, a symlink is refused, only a
    regular file of at most *max_bytes* is read, and the open never blocks (a
    FIFO is refused). Any refusal returns ``{}``.

    Standard library only, apart from :func:`parse_env_file`: ``migrate`` embeds
    the source of both functions in a script that may run on a server with an
    older Dango version.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return {}
    chunks: list[bytes] = []
    size = 0
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return {}
        while size <= max_bytes:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > max_bytes:
        return {}
    values = parse_env_file(b"".join(chunks).decode("utf-8", errors="replace"))
    return {name: values[name] for name in names if values.get(name)}


def _make_spaces_client(spaces_config: dict[str, Any]) -> Any:
    """Create a SpacesClient from config dict, with keys read from the project ``.env``."""
    from dango.platform.cloud.spaces import SpacesClient

    keys = read_env_values(PROJECT_DIR / ".env", SPACES_KEY_NAMES)
    return SpacesClient(
        bucket=spaces_config["bucket"],
        region=spaces_config["region"],
        access_key=keys.get(SPACES_KEY_NAMES[0]),
        secret_key=keys.get(SPACES_KEY_NAMES[1]),
    )


def _copy_file_without_special_bits(src: str, dest: str) -> str:
    """Copy a regular file's contents, then apply its permission bits minus setuid/setgid/sticky.

    Raises ``OSError`` if *src* is a link or not a regular file (opened without following
    a final-component link), so a path swapped after the earlier checks is never read.
    """
    fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {src}")
        with open(dest, "wb") as out:
            shutil.copyfileobj(handle, out, 1024 * 1024)
    os.chmod(dest, stat.S_IMODE(info.st_mode) & 0o777)
    return dest


def _regular_entries_only(root: Path, skipped: list[str], label: str) -> Any:
    """``copytree`` ignore callable: leave out links, special files and ``BACKUP_EXCLUDES``.

    Each link or special file left out is recorded in *skipped* as ``label/<relative path>``.
    """
    from dango.platform.cloud.backup_restore import is_backup_excluded

    def ignore(directory: str, names: list[str]) -> set[str]:
        """Return the entries of *directory* that copytree must not copy."""
        ignored: set[str] = set()
        rel_dir = os.path.relpath(directory, root)
        for name in names:
            if is_backup_excluded(name):
                ignored.add(name)
                continue
            mode = os.lstat(os.path.join(directory, name)).st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                ignored.add(name)
                rel = name if rel_dir == "." else f"{rel_dir}/{name}"
                skipped.append(f"{label}/{rel}")
        return ignored

    return ignore


def _copy_keeping_modes(src: Path, dest: Path, *, step: str) -> None:
    """Copy *src* (file, or directory contents) to *dest* keeping rwx modes, not owners.

    Members keep their source permission bits whatever the umask (a restore onto
    a fresh server recreates files with them; Metabase reads ``data/`` as another
    user), but never setuid/setgid/sticky: root makes these copies.  Directories
    are copied with :func:`_copy_tree_regular`.
    """
    if src.is_dir():
        _copy_tree_regular(src, dest, step=step, skipped=[], label=src.name)
        return
    try:
        _copy_file_without_special_bits(str(src), str(dest))
    except OSError as exc:
        raise CloudProvisioningError(f"Step '{step}' failed: {exc}") from exc


def _copy_tree_regular(src: Path, dest: Path, *, step: str, skipped: list[str], label: str) -> None:
    """Copy directory *src* into *dest*: regular files and directories only, modes kept.

    Links and special files are never followed or read; each is recorded in
    *skipped* as ``label/<path>``.  ``BACKUP_EXCLUDES`` names are left out.  No
    copy keeps setuid/setgid/sticky bits.
    """
    try:
        # Fails (instead of following) if *src* became a link after the caller checked it.
        os.close(os.open(src, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)))
        shutil.copytree(
            src,
            dest,
            symlinks=True,
            ignore=_regular_entries_only(src, skipped, label),
            dirs_exist_ok=True,
            copy_function=_copy_file_without_special_bits,
        )
        # copytree copies directory modes with copystat; drop special bits there too.
        for root, dirs, _files in os.walk(dest):
            for name in [".", *dirs]:
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) & 0o777)
    except (OSError, shutil.Error) as exc:
        raise CloudProvisioningError(f"Step '{step}' failed: {exc}") from exc


def _open_new_private_file(path: Path) -> int:
    """Create *path* as a new 0600 file and return its fd.

    ``O_EXCL`` refuses an existing file and ``O_NOFOLLOW`` a planted link, so root
    never writes through anything another user prepared at a predictable name.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def _write_archive(staging: Path, archive_name: str, archive_path: Path) -> None:
    """Write *staging* as ``<archive_name>/…`` into a new private archive at *archive_path*.

    The archive is written through the fd from :func:`_open_new_private_file`;
    nothing reopens the path by name.
    """
    import tarfile

    fd = _open_new_private_file(archive_path)
    try:
        with (
            os.fdopen(fd, "wb") as handle,
            tarfile.open(fileobj=handle, mode="w:gz", compresslevel=6) as tar,
        ):
            tar.add(str(staging), arcname=archive_name)
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _create_local_archive(
    backup_type: str, *, include_secrets: bool = False
) -> tuple[Path, dict[str, Any], list[str]]:
    """Create a tar.gz backup archive. Stops/restarts services via try/finally."""
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    archive_name = f"backup-{timestamp}"
    archive_path = BACKUP_DIR / f"{archive_name}.tar.gz"
    manifest_path = BACKUP_DIR / f"{archive_name}.json"
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    staging: Path | None = None
    nothing_stopped = False
    try:
        try:
            _stop_services()
        except CloudProvisioningError:
            nothing_stopped = True  # a busy helper: the services are still (or again) running
            raise
        warnings.extend(_checkpoint_databases())
        metabase_vol = _get_metabase_volume_path()
        # Fresh root-only (0700) directory with an unpredictable name.
        staging = Path(tempfile.mkdtemp(prefix="dango-backup."))

        from dango.platform.cloud.backup import (
            BACKUP_DIRS,
            BACKUP_FILES,
            METABASE_CONFIG_FILE,
            SECRET_FILES,
            _write_sanitized_metabase_yaml,
        )

        # Build runtime file list — secrets excluded by default
        file_list = list(BACKUP_FILES)
        if include_secrets:
            file_list.extend(SECRET_FILES)

        from dango.platform.cloud.backup_restore import (
            LINKABLE_DATA_DIR,
            owned_data_target,
            skipped_warning,
        )

        # Root copies only regular files and directories; links and special files
        # in the project are skipped with a warning, never followed or read.
        skipped: list[str] = []
        for fpath in file_list:
            src = PROJECT_DIR / fpath
            if src.is_symlink() or (src.exists() and not src.is_file()):
                skipped.append(fpath)
            elif src.is_file():
                dest = staging / fpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                _copy_keeping_modes(src, dest, step="copy_files")
        metabase_source = PROJECT_DIR / METABASE_CONFIG_FILE
        if metabase_source.exists():
            try:
                _write_sanitized_metabase_yaml(metabase_source, staging / METABASE_CONFIG_FILE)
            except ValueError:
                warnings.append(
                    "Metabase metadata omitted because its YAML could not be safely sanitized"
                )
                _logger.warning("metabase_backup_metadata_omitted")
        for dpath in BACKUP_DIRS:
            src = PROJECT_DIR / dpath
            if src.is_symlink():
                if dpath != LINKABLE_DATA_DIR:
                    skipped.append(dpath)
                    continue
                # data/ may live on separate storage, but only the service user's own.
                target = owned_data_target(src)
                if target is None:
                    raise CloudProvisioningError(
                        f"Refusing to back up {dpath}: it is a symlink whose target is not "
                        "a directory owned by the service user"
                    )
                src = target
            elif not src.exists():
                continue
            elif not src.is_dir():
                skipped.append(dpath)
                continue
            dest = staging / dpath
            dest.mkdir(parents=True, exist_ok=True)
            _copy_tree_regular(src, dest, step="copy_dirs", skipped=skipped, label=dpath)
        for rel in skipped:
            _logger.warning("backup_entry_skipped", path=rel)
            warnings.append(skipped_warning(rel))

        if metabase_vol:
            mb_staging = staging / "metabase"
            mb_staging.mkdir(parents=True, exist_ok=True)
            for h2 in ["metabase.db.mv.db", "metabase.db.trace.db"]:
                src = Path(metabase_vol) / h2
                if src.exists():
                    _copy_keeping_modes(src, mb_staging / h2, step="copy_metabase")
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
        # The archive and manifest are private (the archive may hold secrets); the
        # members inside keep their source modes for restore.
        _write_archive(staging, archive_name, archive_path)
        with os.fdopen(_open_new_private_file(manifest_path), "w") as manifest_file:
            manifest_file.write(manifest_json)
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if not nothing_stopped:
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
        _logger.warning("backup_upload_verification_failed", key=key, exc_info=True)
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


def _load_backup_config() -> dict[str, Any]:
    """Load backup config from cloud.yml. Returns defaults if not configured."""
    cloud_yml = PROJECT_DIR / ".dango" / "cloud.yml"
    if not cloud_yml.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    data: dict[str, Any] = yaml.safe_load(cloud_yml.read_text()) or {}
    return data.get("backup") or {}


def _warn_spaces_reuse(
    spaces_config: dict[str, Any],
    daily_ret: int,
    weekly_ret: int,
    monthly_ret: int,
) -> None:
    """Log a warning if this is the first run and existing backups are found in Spaces."""
    # Only warn on first run (no prior health file or no last_success)
    if HEALTH_FILE.exists():
        try:
            existing = json.loads(HEALTH_FILE.read_text())
            if existing.get("last_success"):
                return  # Not first run
        except (json.JSONDecodeError, OSError):  # noqa: BLE001
            pass

    try:
        client = _make_spaces_client(spaces_config)
        objects = client.list_objects(prefix=SPACES_PREFIX)
        archives = [o for o in objects if o.get("Key", "").endswith(".tar.gz")]
        if archives:
            approx_kept = daily_ret + weekly_ret + monthly_ret
            _logger.warning(
                "spaces_reuse_detected",
                existing_count=len(archives),
                daily_retention=daily_ret,
                weekly_retention=weekly_ret,
                monthly_retention=monthly_ret,
                approx_kept=approx_kept,
                message=(
                    f"Found {len(archives)} existing backups in Spaces bucket "
                    f"from a previous deployment. "
                    f"Current retention ({daily_ret} daily + {weekly_ret} weekly"
                    + (f" + {monthly_ret} monthly" if monthly_ret else "")
                    + f") will keep ~{approx_kept} backups. "
                    f"Older backups will be deleted over time."
                ),
            )
    except Exception:  # noqa: BLE001
        pass  # Best-effort warning — never fail backup for this


def _check_spaces_reuse(
    spaces_config: dict[str, Any],
    daily_ret: int,
    weekly_ret: int,
    monthly_ret: int,
) -> None:
    """Check for existing Spaces backups and write marker file on first run.

    Calls :func:`_warn_spaces_reuse` and then writes a marker file so the
    warning is only shown once per deployment.
    """
    _warn_spaces_reuse(spaces_config, daily_ret, weekly_ret, monthly_ret)
    try:
        MARKER_FILE.parent.mkdir(parents=True, exist_ok=True)
        MARKER_FILE.write_text(
            json.dumps(
                {
                    "initialized": True,
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                }
            )
        )
    except OSError:  # noqa: BLE001
        pass  # Best-effort — never fail backup for marker file write


def _apply_retention(
    spaces_config: dict[str, Any],
    daily_retention: int = 7,
    weekly_retention: int = 4,
    monthly_retention: int = 0,
) -> int:
    """Apply GFS retention policy. Returns number deleted."""
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
        if (now - dt).days < daily_retention:
            keep_keys.add(obj["Key"])

    weekly: dict[tuple[int, int], str] = {}
    for dt, obj in dated:
        if (now - dt).days >= daily_retention:
            iso_year, iso_week, _ = dt.isocalendar()
            weekly[(iso_year, iso_week)] = obj["Key"]
    for wk in sorted(weekly.keys(), reverse=True)[:weekly_retention]:
        keep_keys.add(weekly[wk])

    if monthly_retention > 0:
        monthly: dict[tuple[int, int], str] = {}
        for dt, obj in dated:
            if obj["Key"] not in keep_keys:
                monthly[(dt.year, dt.month)] = obj["Key"]
        for mo in sorted(monthly.keys(), reverse=True)[:monthly_retention]:
            keep_keys.add(monthly[mo])

    if not keep_keys and dated:
        _logger.warning(
            "retention_skip_all",
            daily=daily_retention,
            weekly=weekly_retention,
            monthly=monthly_retention,
            archive_count=len(dated),
            message="Retention would delete all archives — skipping",
        )
        return 0

    deleted = 0
    for _dt, obj in dated:
        if obj["Key"] not in keep_keys:
            try:
                client.delete(obj["Key"])
                client.delete(obj["Key"].replace(".tar.gz", ".json"))
                deleted += 1
            except Exception:
                _logger.debug("backup_rotation_delete_failed", key=obj["Key"], exc_info=True)
    return deleted


def _write_health_status(success: bool, error: str | None = None) -> None:
    """Write backup health status to HEALTH_FILE."""
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if HEALTH_FILE.exists():
        try:
            existing = json.loads(HEALTH_FILE.read_text())
        except (json.JSONDecodeError, OSError):  # noqa: BLE001
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


def _raise_system_exit_on_sigterm(_signum: int, _frame: object) -> None:
    """Turn SIGTERM into ``SystemExit`` so ``finally`` blocks restart services."""
    raise SystemExit(128 + signal.SIGTERM)


def run_scheduled_backup() -> ScheduledBackupResult:
    """Run a full scheduled backup: lock -> archive -> upload -> retention -> health."""
    previous_handler = signal.signal(signal.SIGTERM, _raise_system_exit_on_sigterm)
    try:
        return _run_scheduled_backup()
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


def _run_scheduled_backup() -> ScheduledBackupResult:
    """Body of :func:`run_scheduled_backup` (runs with SIGTERM converted to exit)."""
    start_time = time.monotonic()
    result = ScheduledBackupResult()
    lock_fd = None
    try:
        # Load backup config (defaults if not configured)
        backup_cfg = _load_backup_config()
        include_secrets = bool(backup_cfg.get("include_secrets", False))
        spaces_retention = backup_cfg.get("spaces_retention", {})
        daily_ret = int(spaces_retention.get("daily", DAILY_RETENTION))
        weekly_ret = int(spaces_retention.get("weekly", WEEKLY_RETENTION))
        monthly_ret = int(spaces_retention.get("monthly", 0))

        lock_fd = _acquire_backup_lock()
        archive_path, _manifest, archive_warnings = _create_local_archive(
            backup_type="scheduled", include_secrets=include_secrets
        )
        result.archive_path = str(archive_path)
        result.warnings.extend(archive_warnings)

        try:
            spaces_config = _load_spaces_config()
        except CloudProvisioningError:
            result.warnings.append("Spaces not configured — local-only backup")
            _write_health_status(success=True)
            result.duration_seconds = round(time.monotonic() - start_time, 1)
            return result

        # Spaces reuse warning + marker file on first run
        _check_spaces_reuse(spaces_config, daily_ret, weekly_ret, monthly_ret)

        key = _upload_to_spaces(archive_path, spaces_config)
        result.spaces_key = key
        local_size = archive_path.stat().st_size
        if not _verify_upload(spaces_config, key, local_size):
            result.warnings.append("Upload verification failed — size mismatch")
        deleted = _apply_retention(
            spaces_config,
            daily_retention=daily_ret,
            weekly_retention=weekly_ret,
            monthly_retention=monthly_ret,
        )
        if deleted > 0:
            result.warnings.append(f"Retention: deleted {deleted} old backup(s)")

        # Clean up local archive after successful Spaces upload
        archive_path.unlink(missing_ok=True)
        (BACKUP_DIR / f"{archive_path.stem}.json").unlink(missing_ok=True)

        _write_health_status(success=True)
    except Exception as exc:
        result.error = str(exc)
        _write_health_status(success=False, error=str(exc))
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
            except OSError:  # noqa: BLE001
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
    if PROJECT_DIR.is_symlink():
        raise CloudProvisioningError(
            f"{PROJECT_DIR} is a symlink; refusing to restore into it: nothing was restored"
        )
    name = key.rsplit("/", 1)[-1]
    local_path = BACKUP_DIR / name
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    download_from_spaces(spaces_config, key, local_path)

    from dango.platform.cloud.backup import (
        BACKUP_DIRS,
        BACKUP_FILES,
        DATABASE_FILES,
        METABASE_CONFIG_FILE,
        _write_sanitized_metabase_yaml,
    )
    from dango.platform.cloud.backup_restore import (
        AUTH_CONFIG_FILE,
        AUTH_CONFIG_MAX_BYTES,
        SAFE_EXTRACT_OPTIONS,
        SANITIZED_METABASE_NAME,
        SERVICE_USER,
        UNSAFE_ARCHIVE_MESSAGE,
        auth_config_keeps_auth_on,
        clear_metabase_lock_state_cmd,
        hand_staging_to_service_user_cmd,
        restore_backup_dir_cmd,
        restore_file_cmd,
        set_aside_pipeline_state_cmd,
        unsafe_tar_members,
        unsafe_tree_entries,
        wal_sidecar_names,
    )

    # Refuse links and special files before touching anything (root reads staged files).
    unsafe = unsafe_tar_members(local_path)
    if unsafe:
        raise CloudProvisioningError(UNSAFE_ARCHIVE_MESSAGE.format(names=", ".join(unsafe)))
    # Fresh private staging (unique name in the root-only restore directory), never in /tmp.
    staging = _private_restore_dir("dango-restore-staging")
    private_dir: Path | None = None
    kept_dir: Path | None = None
    try:
        _run_local(
            ["tar", "-xzf", str(local_path), "-C", str(staging), *SAFE_EXTRACT_OPTIONS.split()],
            step="extract",
        )
        # Re-check what was extracted (the archive file could change after the check).
        unsafe = unsafe_tree_entries(staging)
        if unsafe:
            raise CloudProvisioningError(UNSAFE_ARCHIVE_MESSAGE.format(names=", ".join(unsafe)))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    copies_started = False
    nothing_stopped = False
    try:
        try:
            _stop_services()
        except CloudProvisioningError:
            nothing_stopped = True  # a busy helper: the services are still (or again) running
            raise
        # Sanitize Metabase metadata as root into a fresh private directory
        # (never into staging or the project tree), then let the service user read it.
        metabase_source = staging / METABASE_CONFIG_FILE
        sanitized: Path | None = None
        if metabase_source.exists():
            private_dir = _private_restore_dir("dango-restore-meta")
            try:
                _write_sanitized_metabase_yaml(
                    metabase_source, private_dir / SANITIZED_METABASE_NAME
                )
                sanitized = private_dir / SANITIZED_METABASE_NAME
                _run_local(
                    ["chown", f"{SERVICE_USER}:{SERVICE_USER}", str(sanitized)],
                    step="restore_files",
                )
            except ValueError:
                _logger.warning("metabase_restore_metadata_omitted")

        # The old notebooks/ is renamed into a directory no runner can reach, not deleted.
        kept_dir = _private_restore_dir("dango-restore-kept", service_write=True)
        # Project files are written as the service user, which owns the project tree.
        copies_started = True
        _run_builder_script(hand_staging_to_service_user_cmd(str(staging)), step="restore_files")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for db_path in DATABASE_FILES:
            if not (staging / db_path).exists():
                continue
            # A leftover live WAL would be replayed into the restored database.
            # This path takes no safety backup, so keep it aside, not deleted.
            for sidecar in wal_sidecar_names(db_path):
                live = PROJECT_DIR / sidecar
                if live.exists() or live.is_symlink():
                    kept = f"{live}.pre-restore-{stamp}"
                    _run_local(
                        ["runuser", "-u", SERVICE_USER, "--", "mv", "-f", str(live), kept],
                        step="restore_files",
                    )
                    _logger.warning("restore_wal_kept", path=kept)
        _run_builder_script(
            set_aside_pipeline_state_cmd(str(staging), str(PROJECT_DIR), stamp),
            step="restore_files",
        )
        staged_auth = staging / AUTH_CONFIG_FILE
        skip_auth_config = False
        if staged_auth.is_file():
            if staged_auth.stat().st_size > AUTH_CONFIG_MAX_BYTES:
                skip_auth_config = True
            else:
                with open(staged_auth, "rb") as handle:
                    text = handle.read().decode("utf-8", errors="replace")
                skip_auth_config = not auth_config_keeps_auth_on(text)
        if skip_auth_config:
            # A restore never turns authentication off on a server.
            _logger.warning("restore_auth_config_skipped", path=AUTH_CONFIG_FILE)
        for fpath in BACKUP_FILES:
            src = staging / fpath
            if src.exists() and not (fpath == AUTH_CONFIG_FILE and skip_auth_config):
                _run_builder_script(
                    restore_file_cmd(str(src), str(PROJECT_DIR / fpath)),
                    step="restore_files",
                )
        if sanitized is not None:
            _run_builder_script(
                restore_file_cmd(str(sanitized), str(PROJECT_DIR / METABASE_CONFIG_FILE)),
                step="restore_files",
            )
        for dpath in BACKUP_DIRS:
            _run_builder_script(
                restore_backup_dir_cmd(str(staging), str(PROJECT_DIR), dpath, str(kept_dir)),
                step="restore_dirs",
            )

        # Root writes only the Metabase volume, and never through a symlink.
        h2_restored = False
        metabase_vol = _get_metabase_volume_path()
        if metabase_vol and (staging / "metabase").exists():
            for h2 in ["metabase.db.mv.db", "metabase.db.trace.db"]:
                src = staging / "metabase" / h2
                if src.exists():
                    vol = Path(metabase_vol)
                    if vol.is_symlink() or (vol / h2).is_symlink():
                        raise CloudProvisioningError(
                            f"Refusing to write through a symlink: {vol / h2}"
                        )
                    _run_local(["cp", str(src), metabase_vol + "/"], step="restore_metabase")
                    h2_restored = True
        if h2_restored:
            # The restored Metabase database may hold unlocked connection details.
            _run_builder_script(
                clear_metabase_lock_state_cmd(str(PROJECT_DIR)), step="restore_metabase"
            )
        _run_local(["chown", "-R", "dango:dango", "/srv/dango/project"], step="fix_ownership")
        # the restore can recreate protected paths without their denials (services are stopped)
        from dango.platform.cloud import runner_reapply

        for note in runner_reapply.reapply_local():
            print(f"  Warning: {note}", file=sys.stderr)
    except Exception:
        if copies_started:
            # A failed copy leaves a partial, root-owned restore; give it back to
            # the service user before services restart on it.
            with contextlib.suppress(Exception):  # best effort; keep the original error
                subprocess.run(["chown", "-R", "dango:dango", "/srv/dango/project"], timeout=120)
            # notebooks/ may already be recreated without its ACL; put it back before services start
            from dango.platform.cloud import runner_reapply

            with contextlib.suppress(Exception):
                runner_reapply.reapply_local()
        raise
    finally:
        for leftover in (staging, private_dir, kept_dir):
            if leftover is not None:
                shutil.rmtree(leftover, ignore_errors=True)
        if not nothing_stopped:
            _start_services()


def _private_restore_dir(prefix: str, *, service_write: bool = False) -> Path:
    """Fresh ``root:dango 0750`` (``0770`` with *service_write*) directory in the root-only restore directory.

    The parent is ``root:dango 0710`` and never a link, so only root and the service user can
    reach what the restore stages there; the service user still reads the extracted files.
    """
    from dango.platform.cloud import backup_restore
    from dango.platform.cloud.backup_restore import SERVICE_USER

    srv = Path(backup_restore.SRV_ROOT)
    if srv.exists() and (
        srv.is_symlink() or srv.lstat().st_uid != 0 or srv.lstat().st_mode & 0o022
    ):
        raise CloudProvisioningError(
            f"{srv} is not a root-owned directory; refusing to stage a restore"
        )
    root = Path(backup_restore.RESTORE_PRIVATE_ROOT)
    if root.is_symlink():
        raise CloudProvisioningError(f"{root} is a symlink; refusing to stage a restore in it")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _run_local(["chown", f"root:{SERVICE_USER}", str(root)], step="restore_files")
    root.chmod(0o710)
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=root))
    try:
        _run_local(["chown", f"root:{SERVICE_USER}", str(path)], step="restore_files")
        path.chmod(0o770 if service_write else 0o750)
    except Exception:
        shutil.rmtree(path, ignore_errors=True)
        raise
    return path


def enable_scheduled_backup() -> bool:
    """Enable the systemd backup timer. Returns True on success."""
    r = subprocess.run(
        ["systemctl", "enable", "--now", "dango-backup.timer"],
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def disable_scheduled_backup() -> bool:
    """Disable the systemd backup timer. Returns True on success."""
    r = subprocess.run(
        ["systemctl", "disable", "--now", "dango-backup.timer"],
        capture_output=True,
        timeout=30,
    )
    return r.returncode == 0


def is_scheduled_backup_enabled() -> bool:
    """Check if the backup timer is active."""
    r = subprocess.run(
        ["systemctl", "is-active", "dango-backup.timer"],
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
