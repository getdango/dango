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
from typing import TYPE_CHECKING

from dango.exceptions import CloudError, CloudProvisioningError

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_VENV_PIP = "/srv/dango/venv/bin/pip"
_VENV_PYTHON = "/srv/dango/venv/bin/python"
_PROJECT_DIR = "/srv/dango/project"
_CADDYFILE = "/etc/caddy/Caddyfile"
_CADDYFILE_CANDIDATE = "/etc/caddy/Caddyfile.dango-new"
_WS_STATUS = "Caddyfile WebSocket route"
_WS_MANUAL_STEP = (
    "add `forward_auth` to the `/notebooks/marimo/ws` handle, see the 1.0.14 release notes"
)


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

    from dango.platform.cloud.server_status import (
        _get_dango_version,
        check_latest_pypi_version,
    )

    current = _get_dango_version(ssh)
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


def _ws_handles_authenticated(content: str) -> bool:
    """True when every ``/notebooks/marimo/ws`` handle has ``forward_auth``."""
    import re

    found = False
    for m in re.finditer(r"(?m)^([ \t]*)handle /notebooks/marimo/ws \{", content):
        found = True
        end = content.find(f"\n{m.group(1)}}}", m.end())
        body = content[m.end() : end if end != -1 else len(content)]
        if "forward_auth" not in body:
            return False
    return found


def _migrate_caddyfile_ws_route(ssh: SSHManager, warnings: list[str]) -> str:
    """Put ``forward_auth`` in front of the Marimo WebSocket route in place.

    Only the exact handle written by 1.0.13 and earlier is replaced; every
    other byte of the Caddyfile is kept, so domains, HSTS and hand edits
    survive.  Any problem is a warning, never an abort.

    Returns:
        A status line for :class:`UpgradeResult`.
    """
    from dango.platform.cloud._server_templates import LEGACY_MARIMO_WS_RE, marimo_ws_block

    def _skip(reason: str) -> str:
        warnings.append(f"{_CADDYFILE} not updated ({reason}). Manually {_WS_MANUAL_STEP}.")
        return f"{_WS_STATUS} skipped: {reason}"

    read = ssh.exec_command(f"cat {_CADDYFILE}", timeout=30)
    if not read.success:
        return _skip("could not read the file")
    original = read.stdout

    matches = list(LEGACY_MARIMO_WS_RE.finditer(original))
    if not matches and _ws_handles_authenticated(original):
        return f"{_WS_STATUS} already migrated"
    if len(matches) != 1:
        return _skip(
            "the expected WebSocket handle was not found"
            if not matches
            else "the WebSocket handle appears more than once"
        )

    m = matches[0]
    candidate = original[: m.start()] + marimo_ws_block(int(m.group(1))) + original[m.end() :]

    try:
        ssh.write_remote_file(_CADDYFILE_CANDIDATE, candidate, mode=0o644)
        validate = ssh.exec_command(
            f"caddy validate --adapter caddyfile --config {_CADDYFILE_CANDIDATE}", timeout=60
        )
    finally:
        ssh.exec_command(f"rm -f {_CADDYFILE_CANDIDATE}", timeout=30)
    if not validate.success:
        return _skip("caddy validate rejected the updated file")

    from dango.platform.cloud.domain import ensure_caddyfile

    try:
        ensure_caddyfile(ssh, candidate)
    except Exception:
        try:
            ssh.write_remote_file(_CADDYFILE, original, mode=0o644)
        except Exception:
            return _skip(
                "Caddy reload failed and restoring the original file also failed; "
                f"check {_CADDYFILE} and run `systemctl reload-or-restart caddy`"
            )
        reload = ssh.exec_command("systemctl reload-or-restart caddy", timeout=60)
        if reload.success:
            return _skip("Caddy reload failed; the original file was restored and reloaded")
        return _skip(
            "Caddy reload failed; the original file was restored but Caddy did not reload, "
            "run `systemctl reload-or-restart caddy`"
        )
    return f"{_WS_STATUS} migrated"


def upgrade_dango(
    ssh: SSHManager,
    *,
    version: str | None = None,
    skip_backup: bool = False,
    force: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
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

    # 1. Get current version
    _notify(on_progress, "check_version", "running")
    old_version = _get_dango_version(ssh)
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

    # 3. Already at target?
    if old_version == target_version:
        return UpgradeResult(
            old_version=old_version,
            new_version=old_version,
            backup_path=None,
            migrations_run=False,
            docker_rebuilt=False,
            health_check_passed=True,
            duration_seconds=round(time.monotonic() - start_time, 1),
            warnings=["Already at target version — no upgrade performed."],
        )

    # 3b. Downgrade guard
    if old_version is not None and _Version(target_version) < _Version(old_version) and not force:
        raise CloudError(
            f"Target version {target_version} is older than current version {old_version}. "
            "Use --force to downgrade.",
            error_code="DANGO-D022",
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
        start_services,
        stop_services,
        verify_health,
    )

    _notify(on_progress, "stop_services", "running")
    stop_services(ssh)
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

        # 7. Verify new version
        new_version = _get_dango_version(ssh)

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

        # 8b. Authenticate the Marimo WebSocket route on existing servers
        try:
            caddyfile_ws_migration = _migrate_caddyfile_ws_route(ssh, warnings)
        except Exception as exc:
            warnings.append(
                f"{_CADDYFILE} not updated ({type(exc).__name__}). Manually {_WS_MANUAL_STEP}."
            )
            caddyfile_ws_migration = f"{_WS_STATUS} skipped: {type(exc).__name__}"

        # 9. Docker rebuild
        _notify(on_progress, "docker_rebuild", "running")
        docker_rebuilt = False
        compose_project = get_remote_compose_project_name(ssh, _PROJECT_DIR)
        docker_result = ssh.exec_command(
            f"COMPOSE_PROJECT_NAME={compose_project} docker compose "
            f"-f {_PROJECT_DIR}/docker-compose.yml pull "
            f"&& COMPOSE_PROJECT_NAME={compose_project} docker compose "
            f"-f {_PROJECT_DIR}/docker-compose.yml up -d --build metabase </dev/null",
            timeout=600,
        )
        if docker_result.success:
            docker_rebuilt = True
        else:
            warnings.append("Docker rebuild did not complete successfully.")
        _notify(on_progress, "docker_rebuild", "done")
    finally:
        # 10. Start services
        _notify(on_progress, "start_services", "running")
        start_services(ssh)
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
    )
