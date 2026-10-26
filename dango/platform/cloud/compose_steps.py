"""dango/platform/cloud/compose_steps.py

Small building blocks shared by the compose helper switch: running a script as root, the dango-web
unit, the version record and the Metabase rebuild.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from dango.logging import get_logger
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.compose_scripts import PROJECT_DIR, record_script

_logger = get_logger(__name__)

WEB_UNIT_PATH = "/etc/systemd/system/dango-web.service"


def run_script(ssh: Any, script: str, timeout: int) -> Any:
    """Run *script* under ``bash -c`` on the server."""
    return ssh.exec_command(f"bash -c {shlex.quote(script)}", timeout=timeout)


def detail(result: Any) -> str:
    """The error text of a failed command result (stderr, else stdout, else the exit code)."""
    return str((result.stderr or result.stdout or "").strip()) or f"exit {result.exit_code}"


#: SSH timeout for `systemctl stop|restart dango-web`: above the unit's TimeoutStopSec=180.
WEB_SERVICE_TIMEOUT = 190


#: SSH timeout for scripts that take the helper's containers down (lock wait + budget + margin).
LEAVE_TIMEOUT = ch.ROOT_LOCK_WAIT + ch.VERB_BUDGET["down"] + 420


def relay_warnings(result: Any, warnings: list[str]) -> None:
    """Copy the ``RESTORE_WARN:`` lines of a script's output into *warnings*."""
    for line in result.stdout.splitlines():
        if line.startswith("RESTORE_WARN:"):
            warnings.append(line[len("RESTORE_WARN:") :])


def write_record(ssh: Any, version: str, access: str) -> None:
    """Record the server version and Docker access mode; a failure is only logged."""
    result = run_script(ssh, record_script(version, access), 60)
    if not result.success:
        _logger.warning("server_version_record_failed", detail=detail(result))


def rebuild_command(compose_project: str, *, require_helper: bool = False) -> str:
    """Pull and rebuild Metabase: through the helper when it is in use, else the legacy commands.

    With *require_helper* (every 1.0.14 build) there is no legacy branch: without the trusted
    marker the command exits 68.  The default is only for an explicit downgrade below the helper.
    """
    if require_helper:
        helper = shlex.quote(ch.HELPER_PATH)
        return (
            f"if {ch.MARKER_TEST}; then {helper} pull && {helper} up-build; "
            "else echo 'dango-compose: verified Metabase build requires the compose helper' >&2; "
            "exit 68; fi"
        )
    base = (
        f"COMPOSE_PROJECT_NAME={compose_project} docker compose -f {PROJECT_DIR}/docker-compose.yml"
    )
    pull = ch.server_compose_cmd("pull", legacy=f"{base} pull")
    up = ch.server_compose_cmd("up-build", legacy=f"{base} up -d --build metabase </dev/null")
    return f"{pull} && {up}"


def rebuild_timeout(helper: bool) -> int:
    """SSH timeout for :func:`rebuild_command`: both helper calls may wait for the lock."""
    if not helper:
        return 600
    waits = 2 * ch.ROOT_LOCK_WAIT + ch.VERB_BUDGET["pull"] + ch.VERB_BUDGET["up-build"]
    return waits + 2 * ch.CALLER_MARGIN


def rebuild_metabase(ssh: Any, warnings: list[str], *, require_helper: bool = False) -> None:
    """Recreate Metabase (pull and build) through the helper or the legacy commands.

    With *require_helper* a failed rebuild raises instead of adding a warning.
    """
    from dango.exceptions import CloudProvisioningError
    from dango.platform.cloud.backup import get_remote_compose_project_name, helper_branch_active

    project = get_remote_compose_project_name(ssh, PROJECT_DIR)
    result = ssh.exec_command(
        rebuild_command(project, require_helper=require_helper),
        timeout=rebuild_timeout(helper_branch_active(ssh)),
    )
    if not result.success:
        if require_helper:
            raise CloudProvisioningError(f"Docker rebuild failed:\n{detail(result)}")
        warnings.append("Docker rebuild did not complete successfully.")


def upgrade_rebuild(
    ssh: Any, warnings: list[str], compose_project: str, version: str | None
) -> bool:
    """The Docker rebuild of ``remote upgrade``; True when it succeeded.

    From the helper release on it needs the trusted helper and a failure raises (no unverified
    image is built); an explicit downgrade below it keeps the legacy command and a warning.
    """
    from dango.exceptions import CloudProvisioningError
    from dango.platform.cloud.backup import helper_branch_active
    from dango.platform.cloud.compose_versions import helper_capable

    strict = helper_capable(version)
    result = ssh.exec_command(
        rebuild_command(compose_project, require_helper=strict),
        timeout=rebuild_timeout(helper_branch_active(ssh)),
    )
    if result.success:
        return True
    if strict:
        raise CloudProvisioningError(f"Docker rebuild failed:\n{detail(result)}")
    warnings.append("Docker rebuild did not complete successfully.")
    return False


def web_service(ssh: Any, action: str) -> None:
    """``systemctl <action> dango-web``; a failure is only logged."""
    result = ssh.exec_command(f"systemctl {action} dango-web", timeout=WEB_SERVICE_TIMEOUT)
    if not result.success:
        _logger.warning("dango_web_service_failed", action=action, detail=detail(result))


def refresh_web_unit(ssh: Any, warnings: list[str]) -> None:
    """Rewrite an existing dango-web unit (keeping its worker count) when it differs."""
    from dango.platform.cloud._server_templates import build_systemd_unit

    current = ssh.exec_command(f"cat {WEB_UNIT_PATH}", timeout=30)
    if not current.success:
        return
    match = re.search(r"--workers (\d+)", current.stdout)
    unit = build_systemd_unit(workers=int(match.group(1)) if match else None)
    if current.stdout == unit:
        return
    ssh.write_remote_file(WEB_UNIT_PATH, unit, mode=0o644)
    if not ssh.exec_command("systemctl daemon-reload", timeout=60).success:
        warnings.append("systemctl daemon-reload failed after updating dango-web.service")
