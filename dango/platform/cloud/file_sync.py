"""dango/platform/cloud/file_sync.py

Sync local project files to the remote Dango cloud server.

Uses a hybrid approach: SFTP for individual config files (reuses
SSHManager, no subprocess) and rsync for dbt directories (handles
``--delete`` natively for directory sync).

Change detection compares MD5 hashes of dbt model/macro SQL files
before and after sync to identify added, changed, and removed models
for selective dbt rebuilds.

rsync SSH transport
-------------------
rsync uses system SSH, not paramiko.  Since the host was already verified
via paramiko TOFU, rsync bypasses host key checking::

    rsync -avz --delete -e "ssh -i <key> -o StrictHostKeyChecking=no ..." ...

File sync scope
---------------
Synced: ``.dango/sources.yml``, ``.dango/schedules.yml``,
``dbt/models/``, ``dbt/macros/``, ``dbt/dbt_project.yml``,
``dbt/packages.yml``, ``metabase/`` (dashboard exports).

Never synced: ``.env``, ``.dlt/secrets.toml``, ``*.duckdb``,
``metabase.db``, ``dbt/profiles.yml``, ``dbt/target/``, ``__pycache__/``,
``.git/``, ``venv/``.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger

_logger = get_logger(__name__)

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REMOTE_PROJECT_DIR = "/srv/dango/project"

#: Config files to upload via SFTP.  Tuples of (local_relative, remote_relative).
#: Files that may not exist locally are skipped gracefully.
SYNC_CONFIG_FILES: list[tuple[str, str]] = [
    (".dango/sources.yml", f"{REMOTE_PROJECT_DIR}/.dango/sources.yml"),
    (".dango/schedules.yml", f"{REMOTE_PROJECT_DIR}/.dango/schedules.yml"),
    (".dango/monitors.yml", f"{REMOTE_PROJECT_DIR}/.dango/monitors.yml"),
    (".dango/project.yml", f"{REMOTE_PROJECT_DIR}/.dango/project.yml"),
    ("dbt/dbt_project.yml", f"{REMOTE_PROJECT_DIR}/dbt/dbt_project.yml"),
    ("dbt/packages.yml", f"{REMOTE_PROJECT_DIR}/dbt/packages.yml"),
    # Docker build context files — both required to build the Metabase image on the server
    ("docker-compose.yml", f"{REMOTE_PROJECT_DIR}/docker-compose.yml"),
    ("Dockerfile.metabase", f"{REMOTE_PROJECT_DIR}/Dockerfile.metabase"),
    ("entrypoint.sh", f"{REMOTE_PROJECT_DIR}/entrypoint.sh"),
    # Metabase DuckDB driver — synced from local to avoid GitHub download
    # failures on fresh droplets (BUG-124).  ~80MB, acceptable for deploy.
    (
        "metabase-plugins/duckdb.metabase-driver.jar",
        f"{REMOTE_PROJECT_DIR}/metabase-plugins/duckdb.metabase-driver.jar",
    ),
    (
        "metabase-plugins/.driver-version",
        f"{REMOTE_PROJECT_DIR}/metabase-plugins/.driver-version",
    ),
]

#: Directories to sync via rsync (with ``--delete``).
SYNC_DBT_DIRS: list[str] = ["dbt/models", "dbt/macros"]

#: Additional directories to sync via rsync (with ``--delete``).
#: Unlike dbt dirs, these are not part of the change-detection logic.
SYNC_EXTRA_DIRS: list[str] = ["metabase"]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """Result returned by :func:`sync_project_files`."""

    synced_files: list[str] = field(default_factory=list)
    changed_models: list[str] = field(default_factory=list)
    added_models: list[str] = field(default_factory=list)
    removed_models: list[str] = field(default_factory=list)
    packages_changed: bool = False
    has_macro_changes: bool = False
    is_first_deploy: bool = False
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _notify(callback: Callable[[str, str], None] | None, step: str, status: str) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def _compute_remote_hashes(ssh: SSHManager, remote_dir: str, glob_pattern: str) -> dict[str, str]:
    """Compute MD5 hashes of files matching *glob_pattern* under *remote_dir*.

    Returns:
        Dict mapping relative paths (from *remote_dir*) to hex MD5 hashes.
    """
    cmd = f"find {remote_dir} -name '{glob_pattern}' -type f -exec md5sum {{}} \\;"
    result = ssh.exec_command(cmd, timeout=60)
    if not result.success or not result.stdout.strip():
        return {}

    hashes: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            md5_hash, abs_path = parts
            rel_path = abs_path.replace(f"{remote_dir}/", "", 1)
            hashes[rel_path] = md5_hash
    return hashes


def _compute_local_hashes(local_dir: Path, glob_pattern: str) -> dict[str, str]:
    """Compute MD5 hashes of files matching *glob_pattern* under *local_dir*.

    Returns:
        Dict mapping relative paths (from *local_dir*) to hex MD5 hashes.
    """
    if not local_dir.is_dir():
        return {}

    hashes: dict[str, str] = {}
    for fpath in local_dir.rglob(glob_pattern):
        if fpath.is_file():
            md5_hash = hashlib.md5(fpath.read_bytes()).hexdigest()  # noqa: S324
            rel_path = str(fpath.relative_to(local_dir))
            hashes[rel_path] = md5_hash
    return hashes


def _extract_model_name(file_path: str) -> str:
    """Extract the dbt model name from a SQL file path.

    Example: ``"staging/stg_orders.sql"`` -> ``"stg_orders"``
    """
    return Path(file_path).stem


def _detect_dbt_changes(
    before_hashes: dict[str, str],
    local_hashes: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Compare before (remote) and local hashes to detect dbt model changes.

    Returns:
        Tuple of (added_models, changed_models, removed_models).
    """
    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []

    all_paths = set(before_hashes) | set(local_hashes)
    for path in sorted(all_paths):
        remote_hash = before_hashes.get(path)
        local_hash = local_hashes.get(path)
        if remote_hash is None and local_hash is not None:
            added.append(_extract_model_name(path))
        elif remote_hash is not None and local_hash is None:
            removed.append(_extract_model_name(path))
        elif remote_hash != local_hash:
            changed.append(_extract_model_name(path))

    return added, changed, removed


def _build_rsync_ssh_arg(ssh_key_path: Path) -> str:
    """Build the ``-e`` argument for rsync to use the correct SSH key."""
    return f'ssh -i "{ssh_key_path}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'


def _rsync_directory(
    local_dir: Path,
    remote_user_host_dir: str,
    ssh_key_path: Path,
    *,
    delete: bool = True,
    dry_run: bool = False,
) -> str:
    """Sync *local_dir* to *remote_user_host_dir* via rsync.

    Returns:
        rsync stdout output.

    Raises:
        CloudProvisioningError: If rsync exits with a non-zero code.
    """
    cmd: list[str] = [
        "rsync",
        "-avz",
        "-e",
        _build_rsync_ssh_arg(ssh_key_path),
    ]
    if delete:
        cmd.append("--delete")
    if dry_run:
        cmd.append("--dry-run")

    # Trailing slash on source ensures contents are synced, not the dir itself
    cmd.append(f"{local_dir}/")
    cmd.append(remote_user_host_dir)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)  # noqa: S603
    if result.returncode != 0:
        raise CloudProvisioningError(
            f"rsync failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _upload_file_if_exists(
    ssh: SSHManager,
    local_path: Path,
    remote_path: str,
    *,
    dry_run: bool = False,
) -> bool:
    """Upload *local_path* to *remote_path* via SFTP if the local file exists.

    Returns:
        True if the file was uploaded (or would be in dry-run), False if missing.
    """
    if not local_path.is_file():
        return False
    if dry_run:
        return True
    # Skip upload if remote file is identical (MD5 hash check).
    # Avoids re-uploading large files like the ~80MB Metabase DuckDB driver.
    local_hash = hashlib.md5(local_path.read_bytes(), usedforsecurity=False).hexdigest()
    hash_result = ssh.exec_command(f"md5sum {remote_path} 2>/dev/null", timeout=10, check=False)
    if hash_result.success and hash_result.stdout.strip():
        remote_hash = hash_result.stdout.strip().split(None, 1)[0]
        if local_hash == remote_hash:
            return False
    # Ensure remote parent directory exists
    remote_dir = remote_path.rsplit("/", 1)[0]
    mkdir_result = ssh.exec_command(f"mkdir -p {remote_dir}")
    if mkdir_result.exit_code != 0:
        _logger.warning("mkdir_failed", path=remote_dir, stderr=mkdir_result.stderr)
    # Retry SFTP upload on transient network errors (EOFError, timeout)
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            ssh.upload_file(local_path, remote_path)
            return True
        except Exception as e:
            last_err = e
            _logger.warning(
                "sftp_upload_retry", path=str(local_path), attempt=attempt + 1, error=str(e)
            )
            if attempt < 2:
                import time

                time.sleep(2)
    raise last_err  # type: ignore[misc]


def _check_packages_changed(
    ssh: SSHManager,
    local_project_root: Path,
) -> bool:
    """Check whether ``dbt/packages.yml`` content has changed on the remote."""
    local_pkg = local_project_root / "dbt" / "packages.yml"
    if not local_pkg.is_file():
        return False

    local_hash = hashlib.md5(local_pkg.read_bytes()).hexdigest()  # noqa: S324
    remote_path = f"{REMOTE_PROJECT_DIR}/dbt/packages.yml"
    result = ssh.exec_command(f"md5sum {remote_path} 2>/dev/null")
    if not result.success or not result.stdout.strip():
        # File doesn't exist on remote yet — counts as changed
        return True

    remote_hash = result.stdout.strip().split(None, 1)[0]
    return local_hash != remote_hash


def _is_first_deploy(ssh: SSHManager) -> bool:
    """Check whether this is the first deployment (no sources.yml on remote)."""
    result = ssh.exec_command(f"test -f {REMOTE_PROJECT_DIR}/.dango/sources.yml")
    return not result.success


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_project_files(
    ssh: SSHManager,
    local_project_root: Path,
    *,
    remote_host: str | None = None,
    dry_run: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
) -> SyncResult:
    """Sync local project files to the remote server.

    Uses SFTP for individual config files and rsync for dbt directories.
    Detects changes in dbt models/macros for selective rebuilds.

    Args:
        ssh: Connected SSHManager (as root).
        local_project_root: Path to the local Dango project root.
        remote_host: Hostname or IP for rsync transport.  Required unless
            *dry_run* is ``True`` and no dbt directories exist locally.
        dry_run: If True, report what would change without transferring files.
        on_progress: Optional ``(step, status)`` callback for UI updates.

    Returns:
        :class:`SyncResult` with sync details and change detection results.

    Raises:
        CloudProvisioningError: If rsync fails or a critical file is missing.
    """
    if shutil.which("rsync") is None:
        raise CloudProvisioningError(
            "rsync is not installed. Install it with your system package manager "
            "(e.g. brew install rsync on macOS, apt install rsync on Ubuntu)."
        )

    synced_files: list[str] = []
    first_deploy = _is_first_deploy(ssh)

    # --- Step 1: Pre-sync change detection (dbt models + macros) ---
    _notify(on_progress, "detect_changes", "running")
    packages_changed = False

    if first_deploy:
        before_model_hashes: dict[str, str] = {}
        before_macro_hashes: dict[str, str] = {}
    else:
        before_model_hashes = _compute_remote_hashes(
            ssh, f"{REMOTE_PROJECT_DIR}/dbt/models", "*.sql"
        )
        before_macro_hashes = _compute_remote_hashes(
            ssh, f"{REMOTE_PROJECT_DIR}/dbt/macros", "*.sql"
        )
        packages_changed = _check_packages_changed(ssh, local_project_root)

    # Compute local hashes
    local_model_hashes = _compute_local_hashes(local_project_root / "dbt" / "models", "*.sql")
    local_macro_hashes = _compute_local_hashes(local_project_root / "dbt" / "macros", "*.sql")

    _notify(on_progress, "detect_changes", "done")

    # --- Step 2: Upload config files via SFTP ---
    _notify(on_progress, "upload_config", "running")
    for local_rel, remote_abs in SYNC_CONFIG_FILES:
        local_path = local_project_root / local_rel
        uploaded = _upload_file_if_exists(ssh, local_path, remote_abs, dry_run=dry_run)
        if uploaded:
            synced_files.append(local_rel)
    # BUG-124: Fix ownership for metabase-plugins (uploaded as root via SFTP)
    if not dry_run:
        ssh.exec_command(
            f"chown -R dango:dango {REMOTE_PROJECT_DIR}/metabase-plugins 2>/dev/null; true"
        )
    _notify(on_progress, "upload_config", "done")

    # --- Step 3: Sync dbt directories via rsync ---
    _notify(on_progress, "sync_dbt", "running")
    ssh_key_path = ssh.key_path
    for dbt_dir in SYNC_DBT_DIRS:
        local_dir = local_project_root / dbt_dir
        if not local_dir.is_dir():
            continue
        if remote_host is None:
            raise CloudProvisioningError("remote_host is required for rsync-based directory sync")
        remote_dest = f"root@{remote_host}:{REMOTE_PROJECT_DIR}/{dbt_dir}"
        # Ensure remote directory exists before rsync
        if not dry_run:
            mkdir_result = ssh.exec_command(f"mkdir -p {REMOTE_PROJECT_DIR}/{dbt_dir}")
            if mkdir_result.exit_code != 0:
                _logger.warning(
                    "mkdir_failed",
                    path=f"{REMOTE_PROJECT_DIR}/{dbt_dir}",
                    stderr=mkdir_result.stderr,
                )
        _rsync_directory(local_dir, remote_dest, ssh_key_path, delete=True, dry_run=dry_run)
        synced_files.append(f"{dbt_dir}/")
    _notify(on_progress, "sync_dbt", "done")

    # --- Step 3b: Sync extra directories (metabase exports, etc.) ---
    _notify(on_progress, "sync_extra", "running")
    for extra_dir in SYNC_EXTRA_DIRS:
        local_dir = local_project_root / extra_dir
        if not local_dir.is_dir():
            continue
        if remote_host is None:
            raise CloudProvisioningError("remote_host is required for rsync-based directory sync")
        remote_dest = f"root@{remote_host}:{REMOTE_PROJECT_DIR}/{extra_dir}"
        if not dry_run:
            mkdir_result = ssh.exec_command(f"mkdir -p {REMOTE_PROJECT_DIR}/{extra_dir}")
            if mkdir_result.exit_code != 0:
                _logger.warning(
                    "mkdir_failed",
                    path=f"{REMOTE_PROJECT_DIR}/{extra_dir}",
                    stderr=mkdir_result.stderr,
                )
        _rsync_directory(local_dir, remote_dest, ssh_key_path, delete=True, dry_run=dry_run)
        synced_files.append(f"{extra_dir}/")
    _notify(on_progress, "sync_extra", "done")

    # --- Step 4: Detect model and macro changes ---
    has_macro_changes = False
    if first_deploy:
        # All local models are "added" on first deploy
        added_models = sorted(_extract_model_name(p) for p in local_model_hashes)
        changed_models: list[str] = []
        removed_models: list[str] = []
        # Any macros present = macro changes on first deploy
        if local_macro_hashes:
            has_macro_changes = True
        # packages.yml is always "changed" on first deploy if it exists
        if (local_project_root / "dbt" / "packages.yml").is_file():
            packages_changed = True
    else:
        # Model changes — reported individually for selective dbt run
        added_models, changed_models, removed_models = _detect_dbt_changes(
            before_model_hashes, local_model_hashes
        )
        # Macro changes — any change triggers full dbt run (macros aren't
        # individually selectable in dbt run --select)
        added_mac, changed_mac, removed_mac = _detect_dbt_changes(
            before_macro_hashes, local_macro_hashes
        )
        has_macro_changes = bool(added_mac or changed_mac or removed_mac)

    return SyncResult(
        synced_files=synced_files,
        changed_models=changed_models,
        added_models=added_models,
        removed_models=removed_models,
        packages_changed=packages_changed,
        has_macro_changes=has_macro_changes,
        is_first_deploy=first_deploy,
        dry_run=dry_run,
    )
