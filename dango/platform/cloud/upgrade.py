"""dango/platform/cloud/upgrade.py

Remote Dango version upgrade via SSH.

Upgrades the ``getdango`` package on the remote server, runs pending
migrations, rebuilds Docker images, and verifies health.  All functions
require an already-connected ``SSHManager`` (as root).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud.upgrade_caddy import has_ws_auth_endpoint, run_ws_migration
from dango.platform.cloud.upgrade_guard import UpgradeGuard

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_VENV_PIP = "/srv/dango/venv/bin/pip"
_VENV_PYTHON = "/srv/dango/venv/bin/python"
_PROJECT_DIR = "/srv/dango/project"

#: Progress messages for the ``on_progress`` steps of :func:`upgrade_dango`.
PROGRESS_LABELS: dict[str, str] = {
    "rebuild_venv": "Checking the server venv...",
    "check_version": "Checking current version...",
    "check_pypi": "Checking latest PyPI version...",
    "backup": "Creating pre-upgrade backup...",
    "stop_services": "Stopping services...",
    "pip_install": "Installing new version...",
    "migrations": "Running migrations...",
    "runtime_ownership": "Securing the server runtime...",
    "docker_access": "Switching Docker access...",
    "docker_rebuild": "Rebuilding Docker images...",
    "start_services": "Starting services...",
    "verify_health": "Verifying health...",
}


@dataclass
class UpgradeResult:
    """Result returned by :func:`upgrade_dango`."""

    old_version: str | None
    new_version: str | None
    backup_path: str | None
    migrations_run: bool
    docker_rebuilt: bool
    health_check_passed: bool
    duration_seconds: float
    warnings: list[str] = field(default_factory=list)
    caddyfile_ws_migration: str | None = None
    runtime_ownership: str | None = None
    venv_rebuild: str | None = None
    docker_access: str | None = None


def validate_version_string(version: str) -> None:
    """Validate a version string is PEP 440 compliant.

    Raises:
        CloudError: If *version* is not a valid PEP 440 version.
    """
    from packaging.version import InvalidVersion, Version

    try:
        Version(version)
    except InvalidVersion:
        raise CloudError(
            f"Invalid version string: {version!r}. "
            "Expected a PEP 440 version (e.g. 1.0.0, 1.0.0b4, 1.0.0rc1).",
            error_code="DANGO-D020",
        ) from None


def check_versions(ssh: SSHManager) -> tuple[str | None, str | None]:
    """Return ``(current_remote_version, latest_pypi_version)``.

    Uses ``_get_dango_version()`` from ``server_status`` for the remote
    version and ``check_latest_pypi_version()`` for the PyPI version.
    When the current remote version is a pre-release, ``include_pre=True``
    is passed so that PyPI returns the latest pre-release rather than the
    latest stable version.
    """
    from packaging.version import Version

    from dango.platform.cloud import runtime_ownership as ro
    from dango.platform.cloud.server_status import (
        _get_dango_version,
        check_latest_pypi_version,
    )

    # An untrusted venv is never run: read its recorded version as data instead.
    if ro.venv_is_untrusted(ssh):
        current = ro.installed_version_from_metadata(ssh)
    else:
        current = ro.valid_version(_get_dango_version(ssh))
    include_pre = current is not None and Version(current).is_prerelease
    latest = check_latest_pypi_version(include_pre=include_pre)
    return current, latest


def _run_checked(ssh: SSHManager, command: str, *, step: str, timeout: int = 120) -> str:
    """Run *command* via SSH, raising ``CloudProvisioningError`` on failure."""
    result = ssh.exec_command(command, timeout=timeout)
    if not result.success:
        raise CloudProvisioningError(
            f"Upgrade step '{step}' failed (exit {result.exit_code}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result.stdout


def _notify(callback: Callable[[str, str], None] | None, step: str, status: str) -> None:
    """Call the progress callback if provided."""
    if callback is not None:
        callback(step, status)


def _switch_at_target(
    ssh: SSHManager, warnings: list[str], version: str | None, guard: UpgradeGuard
) -> str | None:
    """At the target version: finish the switch to the compose helper if it is not settled.

    Only a server that is not yet settled on the helper has dango-web stopped (for the group
    removal and process drop) and Metabase recreated; a settled server is not touched.
    """
    if not cm.helper_capable(version) or not cm.needs_migration(ssh):
        return None
    guard.arm()  # dango-web is stopped inside the migration
    try:
        return cm.migrate_docker_access(
            ssh,
            warnings,
            version=version or "",
            on_step=guard.rearm,
            manage_web=True,
            rebuild=True,
        )
    finally:
        guard.disarm()


def upgrade_dango(
    ssh: SSHManager,
    *,
    version: str | None = None,
    skip_backup: bool = False,
    force: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
    local_project_root: Path | None = None,
    byos: bool = False,
) -> UpgradeResult:
    """Upgrade Dango on the remote server.

    Workflow:
        1. Get current version
        2. Determine target version (specific or latest from PyPI)
        3. Create pre-upgrade backup (unless *skip_backup*)
        4. Stop services
        5. pip install target version
        6. Run migrations
        7. Rebuild Docker images
        8. Start services + verify health

    Args:
        ssh: Connected ``SSHManager`` (as root).
        version: Specific version to install (e.g. ``"1.2.3"``).
            If ``None``, upgrades to the latest PyPI version.
        skip_backup: If ``True``, skip the pre-upgrade backup.
        on_progress: Optional ``(step, status)`` callback.
        local_project_root: Local project; its requirements files are reinstalled
            if the server venv has to be rebuilt as root.
        byos: Server not provisioned by Dango: a failed ownership check warns
            instead of failing the upgrade.

    Returns:
        ``UpgradeResult`` with version change details and health status.

    Raises:
        CloudError: If the version string is invalid.
        CloudProvisioningError: If any SSH command fails.
    """
    from dango.platform.cloud.server_status import (
        _get_dango_version,
        check_latest_pypi_version,
    )

    start_time = time.monotonic()
    warnings: list[str] = []
    guard = UpgradeGuard(ssh, warnings)

    # 0. Never run a venv another user could write: check it as data first
    from dango.platform.cloud import runtime_ownership as ro

    try:
        untrusted = ro.venv_is_untrusted(ssh)
    except Exception as exc:
        raise CloudError(ro.REBUILD_ABORT.format(f"the venv could not be checked: {exc}")) from exc

    # 1. Get current version (from the venv metadata, as data, when it is untrusted)
    _notify(on_progress, "check_version", "running")
    if untrusted:
        old_version = ro.trusted_metadata_version(ssh, version)
    else:
        old_version = ro.valid_version(_get_dango_version(ssh))
    _notify(on_progress, "check_version", "done")

    # 2. Determine target version
    from packaging.version import Version as _Version

    _is_pre = old_version is not None and _Version(old_version).is_prerelease
    if version is not None:
        validate_version_string(version)
        target_version = version
        if _Version(version).is_prerelease:
            _is_pre = True
    else:
        _notify(on_progress, "check_pypi", "running")
        latest = check_latest_pypi_version(include_pre=_is_pre)
        _notify(on_progress, "check_pypi", "done")
        if latest is None:
            raise CloudError(
                "Could not determine latest version from PyPI. "
                "Specify a version explicitly with --version.",
                error_code="DANGO-D021",
            )
        target_version = latest

    # 2b. Client/server version notices (an older client does not know the compose helper)
    from dango.platform.cloud.server_status import local_client_version

    local_version = local_client_version()
    if line := cm.client_upgrade_line(local_version, target_version):
        warnings.append(line)
    if notice := cm.client_version_notice(ssh, local_version):
        warnings.append(notice)

    # 3b. Downgrade guard
    if old_version is not None and _Version(target_version) < _Version(old_version) and not force:
        raise CloudError(
            f"Target version {target_version} is older than current version {old_version}. "
            "Use --force to downgrade.",
            error_code="DANGO-D022",
        )

    # 3c. Rebuild an untrusted venv at the target version before anything runs it
    venv_rebuild: str | None = None
    if untrusted:
        _notify(on_progress, "rebuild_venv", "running")
        guard.arm()  # the rebuild stops services; a dead client must not leave them down
        try:
            venv_rebuild = ro.rebuild_untrusted_venv(
                ssh,
                warnings,
                version=target_version,
                local_project_root=local_project_root,
                on_step=guard.rearm,
            )
        except ro.VenvRebuildError as exc:
            raise CloudError(ro.REBUILD_ABORT.format(exc), error_code="DANGO-D023") from exc
        finally:
            guard.disarm()
        _notify(on_progress, "rebuild_venv", "done")

    # 3. Already at target?  Still finish the Caddyfile WebSocket migration
    # (idempotent) so a re-run can complete a step an earlier run skipped.
    if old_version == target_version:
        at_target_warnings = ["Already at target version — no upgrade performed.", *warnings]
        ws_status = (
            run_ws_migration(ssh, at_target_warnings, old_version)
            if has_ws_auth_endpoint(old_version)
            else None
        )
        ownership_status = ro.secure_runtime(
            ssh,
            at_target_warnings,
            installed_version=old_version,
            strict=not byos,
            extra_steps=cm.ownership_steps() if cm.helper_capable(old_version) else (),
        )
        at_target_access = _switch_at_target(ssh, at_target_warnings, old_version, guard)
        return UpgradeResult(
            old_version=old_version,
            new_version=old_version,
            backup_path=None,
            migrations_run=False,
            docker_rebuilt=False,
            health_check_passed=True,
            duration_seconds=round(time.monotonic() - start_time, 1),
            warnings=at_target_warnings,
            caddyfile_ws_migration=ws_status,
            runtime_ownership=ownership_status,
            venv_rebuild=venv_rebuild,
            docker_access=at_target_access,
        )

    # 4. Pre-upgrade backup
    backup_path: str | None = None
    if not skip_backup:
        from dango.platform.cloud.backup import create_backup

        _notify(on_progress, "backup", "running")
        backup_result = create_backup(ssh, backup_type="pre-upgrade")
        backup_path = backup_result.archive_path
        _notify(on_progress, "backup", "done")
    else:
        warnings.append("Pre-upgrade backup skipped (--skip-backup).")

    # 5. Stop services
    from dango.platform.cloud.backup import (
        get_remote_compose_project_name,
        helper_branch_active,
        start_services,
        stop_services,
        verify_health,
    )

    _notify(on_progress, "stop_services", "running")
    guard.arm()  # before the stop: a killed client leaves a timer that starts dango-web
    try:
        # Each helper stop can wait for the lock: the guard window restarts between the two.
        stop_services(ssh, on_first_stop=guard.rearm)
    except BaseException:
        guard.disarm()  # nothing is left stopped (stop_services starts everything again)
        raise
    guard.rearm()
    _notify(on_progress, "stop_services", "done")

    try:
        # 6. pip install
        _notify(on_progress, "pip_install", "running")
        pre_flag = "--pre " if _is_pre else ""
        if version is not None:
            pip_cmd = f"{_VENV_PIP} install {pre_flag}getdango=={target_version}"
        else:
            pip_cmd = f"{_VENV_PIP} install {pre_flag}--upgrade getdango"
        _run_checked(ssh, pip_cmd, step="pip_install", timeout=300)
        _notify(on_progress, "pip_install", "done")
        guard.rearm()

        # 7. Verify new version (validated before it is used anywhere)
        new_version = ro.valid_version(_get_dango_version(ssh))

        # 8. Run migrations
        _notify(on_progress, "migrations", "running")
        migrations_run = False
        migrate_result = ssh.exec_command(
            f'sudo -u dango {_VENV_PYTHON} -c "'
            "from pathlib import Path; "
            "from dango.migrations import apply_all_pending; "
            f"apply_all_pending(Path('{_PROJECT_DIR}'))\" 2>&1",
            timeout=120,
        )
        if migrate_result.success:
            migrations_run = True
        else:
            warnings.append(
                f"Migration command exited with code {migrate_result.exit_code}: "
                f"{migrate_result.stderr.strip() or migrate_result.stdout.strip()}"
            )
        _notify(on_progress, "migrations", "done")
        guard.rearm()

        # 8b. Authenticate the Marimo WebSocket route on existing servers
        caddyfile_ws_migration = run_ws_migration(ssh, warnings, new_version)

        # 8c. Root-owned runtime (the venv was already rebuilt in step 0 if needed)
        _notify(on_progress, "runtime_ownership", "running")
        runtime_ownership = ro.secure_runtime(
            ssh,
            warnings,
            installed_version=new_version,
            strict=not byos,
            extra_steps=cm.ownership_steps() if cm.helper_capable(new_version) else (),
        )
        _notify(on_progress, "runtime_ownership", "done")
        guard.rearm()

        # 8d. Docker access: the compose helper from 1.0.14 on; a downgrade below it gets the
        # docker group (and the Metabase data ownership) back
        docker_access: str | None = None
        if cm.helper_capable(new_version):
            _notify(on_progress, "docker_access", "running")
            cm.refresh_web_unit(ssh, warnings)
            docker_access = cm.migrate_docker_access(
                ssh, warnings, version=new_version or target_version, on_step=guard.rearm
            )
            _notify(on_progress, "docker_access", "done")
        elif cm.helper_capable(old_version) and cm.below_helper_version(new_version):
            _notify(on_progress, "docker_access", "running")
            cm.restore_docker_access(ssh, warnings, version=new_version or target_version)
            docker_access = cm.ACCESS_GROUP
            _notify(on_progress, "docker_access", "done")
        guard.rearm()

        # 9. Docker rebuild
        _notify(on_progress, "docker_rebuild", "running")
        docker_rebuilt = False
        compose_project = get_remote_compose_project_name(ssh, _PROJECT_DIR)
        docker_result = ssh.exec_command(
            cm.rebuild_command(compose_project),
            timeout=cm.rebuild_timeout(helper_branch_active(ssh)),
        )
        if docker_result.success:
            docker_rebuilt = True
        else:
            warnings.append("Docker rebuild did not complete successfully.")
        _notify(on_progress, "docker_rebuild", "done")
        guard.rearm()
    finally:
        # 10. Start services, then the client-side restart makes the guard redundant
        _notify(on_progress, "start_services", "running")
        try:
            start_services(ssh)
        finally:
            guard.disarm()
        _notify(on_progress, "start_services", "done")

    # 11. Verify health
    _notify(on_progress, "verify_health", "running")
    health_ok = verify_health(ssh)
    if not health_ok:
        warnings.append(
            "Health check did not pass within 90 seconds. "
            "Run 'dango remote rollback' to restore the previous version."
        )
    _notify(on_progress, "verify_health", "done")

    return UpgradeResult(
        old_version=old_version,
        new_version=new_version,
        backup_path=backup_path,
        migrations_run=migrations_run,
        docker_rebuilt=docker_rebuilt,
        health_check_passed=health_ok,
        duration_seconds=round(time.monotonic() - start_time, 1),
        warnings=warnings,
        caddyfile_ws_migration=caddyfile_ws_migration,
        runtime_ownership=runtime_ownership,
        venv_rebuild=venv_rebuild,
        docker_access=docker_access,
    )
