"""dango/platform/cloud/deployer.py

Full push deployment workflow for Dango cloud deployments.

Orchestrates file sync, pre-deploy backup, dbt operations, and service
management into a single ``push_deploy()`` function.  Uses a deploy lock
to prevent concurrent deployments.

Deploy lock
-----------
Lock file at ``/srv/dango/deploy.lock`` contains JSON with deployer
identity, start time, and expiry (30 minutes).  ``--force`` overrides
any existing lock.  Lock is always released in a ``finally`` block.

Push workflow
-------------
1. Acquire deploy lock
2. Create pre-deploy backup (stops/starts services internally)
3. Stop dango-web service (DuckDB single-writer constraint)
4. Sync files (via ``file_sync.sync_project_files``)
5. Fix file ownership (rsync runs as root, creates root-owned files)
6. Validate sources (check credentials exist on remote)
7. ``dbt deps`` (if packages.yml changed)
8. ``dbt compile`` (abort on failure)
9. ``dbt run --select <changed models>`` (if any)
10. Start dango-web service
11. Release deploy lock
12. Report results

Error handling: ``finally`` block always releases the lock and restarts
dango-web.  On failure, the pre-deploy backup is available for
``dango remote rollback``.
"""

from __future__ import annotations

import json
import platform
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dango.exceptions import CloudProvisioningError

if TYPE_CHECKING:
    from dango.platform.cloud.backup import BackupResult
    from dango.platform.cloud.file_sync import SyncResult
    from dango.platform.cloud.ssh import SSHManager

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEPLOY_LOCK_PATH = "/srv/dango/deploy.lock"
LOCK_TIMEOUT_MINUTES = 30
REMOTE_PROJECT_DIR = "/srv/dango/project"
VENV_BIN = "/srv/dango/venv/bin"
DBT_PROJECT_DIR = "/srv/dango/project/dbt"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployLock:
    """Metadata about an active deploy lock."""

    deployer: str
    started_at: str
    expires_at: str


@dataclass
class DeployResult:
    """Result returned by :func:`push_deploy`."""

    sync_result: SyncResult
    backup_result: BackupResult | None = None
    dbt_deps_run: bool = False
    dbt_compile_success: bool = False
    models_rebuilt: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _notify(callback: Callable[[str, str], None] | None, step: str, status: str) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def _get_deployer_identity() -> str:
    """Return a human-readable deployer identity string."""
    import getpass

    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    host = platform.node() or "unknown"
    return f"{user}@{host}"


def _check_existing_lock(ssh: SSHManager) -> DeployLock | None:
    """Read the deploy lock file from the remote server.

    Returns:
        ``DeployLock`` if a lock file exists, ``None`` otherwise.
    """
    result = ssh.exec_command(f"cat {DEPLOY_LOCK_PATH} 2>/dev/null")
    if not result.success or not result.stdout.strip():
        return None

    try:
        data: dict[str, Any] = json.loads(result.stdout.strip())
        return DeployLock(
            deployer=data.get("deployer", "unknown"),
            started_at=data.get("started_at", ""),
            expires_at=data.get("expires_at", ""),
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _is_lock_expired(lock: DeployLock) -> bool:
    """Return True if the lock has passed its expiration time."""
    if not lock.expires_at:
        return True
    try:
        expires = datetime.fromisoformat(lock.expires_at)
        return datetime.now(tz=timezone.utc) > expires
    except ValueError:
        return True


def _acquire_lock(ssh: SSHManager, *, force: bool = False) -> DeployLock:
    """Acquire the deploy lock on the remote server.

    Args:
        ssh: Connected SSHManager.
        force: Override any existing lock.

    Returns:
        The newly created :class:`DeployLock`.

    Raises:
        CloudProvisioningError: If a valid lock exists and *force* is False.
    """
    existing = _check_existing_lock(ssh)
    if existing is not None and not force:
        if not _is_lock_expired(existing):
            raise CloudProvisioningError(
                f"Deploy lock held by {existing.deployer} "
                f"(started {existing.started_at}). "
                "Use --force to override."
            )

    now = datetime.now(tz=timezone.utc)
    from datetime import timedelta

    expires = now + timedelta(minutes=LOCK_TIMEOUT_MINUTES)

    lock = DeployLock(
        deployer=_get_deployer_identity(),
        started_at=now.isoformat(),
        expires_at=expires.isoformat(),
    )
    lock_json = json.dumps(
        {
            "deployer": lock.deployer,
            "started_at": lock.started_at,
            "expires_at": lock.expires_at,
        }
    )

    ssh.exec_command(f"mkdir -p {Path(DEPLOY_LOCK_PATH).parent}")
    result = ssh.exec_command(f"cat > {DEPLOY_LOCK_PATH} << 'LOCK_EOF'\n{lock_json}\nLOCK_EOF")
    if not result.success:
        raise CloudProvisioningError(f"Failed to write deploy lock: {result.stderr.strip()}")

    return lock


def _release_lock(ssh: SSHManager) -> None:
    """Remove the deploy lock file from the remote server."""
    ssh.exec_command(f"rm -f {DEPLOY_LOCK_PATH}")


def _stop_web_service(ssh: SSHManager) -> None:
    """Stop the dango-web systemd service."""
    ssh.exec_command("systemctl stop dango-web || true", timeout=60)


def _start_web_service(ssh: SSHManager) -> None:
    """Start the dango-web systemd service."""
    ssh.exec_command("systemctl start dango-web || true", timeout=60)


def _validate_remote_sources(ssh: SSHManager) -> list[str]:
    """Validate that remote sources have corresponding credentials.

    Parses ``.dango/sources.yml`` on the remote and checks that
    ``.dlt/secrets.toml`` has entries for each source.

    Returns:
        List of error messages.  Empty list means all sources are valid.
    """
    errors: list[str] = []

    # Read sources.yml
    sources_result = ssh.exec_command(f"cat {REMOTE_PROJECT_DIR}/.dango/sources.yml 2>/dev/null")
    if not sources_result.success or not sources_result.stdout.strip():
        return errors  # No sources configured — nothing to validate

    # Read secrets.toml
    secrets_result = ssh.exec_command(f"cat {REMOTE_PROJECT_DIR}/.dlt/secrets.toml 2>/dev/null")
    secrets_content = secrets_result.stdout if secrets_result.success else ""

    # Parse source names from sources.yml (look for name: fields)
    try:
        import yaml

        sources_data: dict[str, Any] = yaml.safe_load(sources_result.stdout) or {}
    except Exception:
        # If YAML parsing fails, skip validation
        return errors

    sources = sources_data.get("sources", [])
    if not isinstance(sources, list):
        return errors

    for source in sources:
        if not isinstance(source, dict):
            continue
        name = source.get("name", "")
        if name and f"[sources.{name}]" not in secrets_content:
            errors.append(f"Source '{name}' has no credentials in .dlt/secrets.toml on the server")

    return errors


def _run_remote_dbt(
    ssh: SSHManager,
    subcommand: str,
    extra_args: str = "",
) -> Any:
    """Run a dbt command on the remote server as the ``dango`` user.

    Returns:
        ``CommandResult`` from the SSH command.
    """
    cmd = (
        f"sudo -u dango {VENV_BIN}/dbt {subcommand}"
        f" --project-dir {DBT_PROJECT_DIR}"
        f" --profiles-dir {DBT_PROJECT_DIR}"
    )
    if extra_args:
        cmd = f"{cmd} {extra_args}"
    return ssh.exec_command(cmd, timeout=600)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def push_deploy(
    ssh: SSHManager,
    local_project_root: Path,
    remote_host: str,
    *,
    dry_run: bool = False,
    force: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
) -> DeployResult:
    """Execute full push deployment workflow.

    Syncs local project files to the remote server, runs dbt operations
    for changed models, and manages service lifecycle.

    Args:
        ssh: Connected SSHManager (as root).
        local_project_root: Path to the local Dango project root.
        remote_host: Hostname or IP for rsync transport.
        dry_run: If True, show what would change without applying.
        force: Override any existing deploy lock.
        on_progress: Optional ``(step, status)`` callback for UI updates.

    Returns:
        :class:`DeployResult` with deployment details.

    Raises:
        CloudProvisioningError: On lock conflict, dbt compile failure,
            or other deployment errors.
    """
    from dango.platform.cloud.backup import create_backup
    from dango.platform.cloud.file_sync import SyncResult, sync_project_files

    start_time = time.monotonic()
    warnings: list[str] = []
    backup_result: BackupResult | None = None
    dbt_deps_run = False
    dbt_compile_success = False
    models_rebuilt: list[str] = []
    sync_result = SyncResult(dry_run=dry_run)

    if dry_run:
        # --- Dry-run mode ---
        _notify(on_progress, "sync_files", "running")
        sync_result = sync_project_files(
            ssh,
            local_project_root,
            remote_host=remote_host,
            dry_run=True,
            on_progress=on_progress,
        )
        _notify(on_progress, "sync_files", "done")

        return DeployResult(
            sync_result=sync_result,
            backup_result=None,
            dbt_deps_run=sync_result.packages_changed,
            dbt_compile_success=False,
            models_rebuilt=[],
            duration_seconds=round(time.monotonic() - start_time, 1),
            warnings=warnings,
            dry_run=True,
        )

    # --- Full deploy ---
    lock: DeployLock | None = None
    web_stopped = False

    try:
        # Step 1: Acquire deploy lock
        _notify(on_progress, "acquire_lock", "running")
        lock = _acquire_lock(ssh, force=force)
        _notify(on_progress, "acquire_lock", "done")

        # Step 2: Pre-deploy backup (stops/starts services internally)
        _notify(on_progress, "create_backup", "running")
        backup_result = create_backup(ssh, backup_type="pre-deploy", on_progress=on_progress)
        if backup_result.warnings:
            warnings.extend(backup_result.warnings)
        _notify(on_progress, "create_backup", "done")

        # Step 3: Stop dango-web (DuckDB single-writer constraint)
        _notify(on_progress, "stop_web", "running")
        _stop_web_service(ssh)
        web_stopped = True
        _notify(on_progress, "stop_web", "done")

        # Step 4: Sync files
        _notify(on_progress, "sync_files", "running")
        sync_result = sync_project_files(
            ssh, local_project_root, remote_host=remote_host, on_progress=on_progress
        )
        _notify(on_progress, "sync_files", "done")

        # Step 5: Fix file ownership
        _notify(on_progress, "fix_ownership", "running")
        ssh.exec_command(f"chown -R dango:dango {REMOTE_PROJECT_DIR}", timeout=60)
        _notify(on_progress, "fix_ownership", "done")

        # Step 6: Validate sources
        _notify(on_progress, "validate_sources", "running")
        source_errors = _validate_remote_sources(ssh)
        if source_errors:
            for err in source_errors:
                warnings.append(err)
        _notify(on_progress, "validate_sources", "done")

        # Step 7: dbt deps (if packages.yml changed)
        if sync_result.packages_changed:
            _notify(on_progress, "dbt_deps", "running")
            deps_result = _run_remote_dbt(ssh, "deps")
            dbt_deps_run = True
            if not deps_result.success:
                warnings.append(
                    f"dbt deps failed: {deps_result.stderr.strip() or deps_result.stdout.strip()}"
                )
            _notify(on_progress, "dbt_deps", "done")

        # Step 8: dbt compile
        _notify(on_progress, "dbt_compile", "running")
        compile_result = _run_remote_dbt(ssh, "compile")
        if not compile_result.success:
            raise CloudProvisioningError(
                f"dbt compile failed: {compile_result.stderr.strip() or compile_result.stdout.strip()}"
            )
        dbt_compile_success = True
        _notify(on_progress, "dbt_compile", "done")

        # Step 9: dbt run (if models changed)
        all_changed = sync_result.added_models + sync_result.changed_models
        if all_changed:
            _notify(on_progress, "dbt_run", "running")
            select_arg = " ".join(all_changed)
            run_result = _run_remote_dbt(ssh, "run", f"--select {select_arg}")
            if run_result.success:
                models_rebuilt = list(all_changed)
            else:
                warnings.append(
                    f"dbt run failed: {run_result.stderr.strip() or run_result.stdout.strip()}"
                )
            _notify(on_progress, "dbt_run", "done")

    finally:
        # Always restart web service and release lock
        if web_stopped:
            _notify(on_progress, "start_web", "running")
            _start_web_service(ssh)
            _notify(on_progress, "start_web", "done")

        if lock is not None:
            _release_lock(ssh)

    return DeployResult(
        sync_result=sync_result,
        backup_result=backup_result,
        dbt_deps_run=dbt_deps_run,
        dbt_compile_success=dbt_compile_success,
        models_rebuilt=models_rebuilt,
        duration_seconds=round(time.monotonic() - start_time, 1),
        warnings=warnings,
        dry_run=False,
    )
