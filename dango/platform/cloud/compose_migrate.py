"""dango/platform/cloud/compose_migrate.py

Switching a server's Docker access from the ``docker`` group to the root-owned compose helper.

Every step is one SSH call that checks before it acts.  The order keeps one invariant at every
interruption point: either the marker is absent and the service account is still in the ``docker``
group (the old working state), or the marker is present and the helper was installed, self-tested,
pinned, rendered and verified (the new working state).  The marker is written last; the group is
removed only after it; processes that started with the group are ended after that.

Any failure before the switch leaves the old state and returns the fallback (``group``) with a loud
warning; the next ``remote upgrade`` retries.  Downgrading below 1.0.14 restores group access.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from typing import Any

from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.compose_scripts import (
    ACCESS_GROUP,
    ACCESS_HELPER,
    ACCESS_MIGRATING,
    ID_RE,
    PROJECT_DIR,
    consistency_script,
    drop_group_script,
    fallback_script,
    marker_script,
    ownership_steps,
    prechecks_script,
    record_script,
    release_of,
    render_script,
    restore_script,
    self_test_script,
    terminate_script,
    verify_script,
)

__all__ = [
    "ACCESS_GROUP",
    "ACCESS_HELPER",
    "ACCESS_MIGRATING",
    "PROJECT_DIR",
    "below_helper_version",
    "client_upgrade_line",
    "client_version_notice",
    "drop_group_script",
    "enable_fresh",
    "enter_fallback",
    "ensure_pinned",
    "fallback_script",
    "helper_capable",
    "install_helper",
    "marker_script",
    "migrate_docker_access",
    "needs_migration",
    "ownership_steps",
    "pin_and_render",
    "prebuild_images",
    "prechecks_script",
    "rebuild_command",
    "rebuild_timeout",
    "record_script",
    "refresh_web_unit",
    "restore_docker_access",
    "restore_script",
    "self_test_script",
    "server_version",
    "terminate_script",
]

_logger = get_logger(__name__)

#: First release that serves and understands the compose helper.
MIN_RELEASE = (1, 0, 14)
WEB_UNIT_PATH = "/etc/systemd/system/dango-web.service"
#: Helper exits that mean "the helper refused or is unusable", as opposed to an ordinary failure.
HELPER_REFUSALS = frozenset({64, 65, 66, 67, 75})
#: SSH timeout for the render script (helper render budget + sudo lock wait + margin).
RENDER_TIMEOUT = ch.SUDO_LOCK_WAIT + ch.VERB_BUDGET["render"] + 2 * ch.CALLER_MARGIN

_RETRY_HINT = "Run `dango remote upgrade` to retry once the cause is fixed."


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def helper_capable(version: str | None) -> bool:
    """True when *version* (a pre-release of 1.0.14 included) ships the compose helper."""
    release = release_of(version)
    return release is not None and release >= MIN_RELEASE


def below_helper_version(version: str | None) -> bool:
    """True when *version* predates the compose helper (a downgrade target that needs the group)."""
    release = release_of(version)
    return release is not None and release < MIN_RELEASE


def client_upgrade_line(local_version: str, target_version: str) -> str | None:
    """The line shown before a server upgrade when the local client is older than the target."""
    from packaging.version import InvalidVersion, Version

    try:
        if Version(local_version) >= Version(target_version):
            return None
    except InvalidVersion:
        return None
    return (
        "Upgrade your local Dango (pip install -U getdango) before upgrading the server; "
        f"this client is {local_version}, the target is {target_version}."
    )


def client_version_notice(ssh: Any, local_version: str) -> str | None:
    """Warning when the server records a newer Dango than this client (never raises)."""
    from dango.platform.cloud.server_status import _get_docker_access, client_version_warning

    try:
        _access, recorded = _get_docker_access(ssh)
        return client_version_warning(recorded, local_version)
    except Exception:
        _logger.warning("client_version_notice_failed", exc_info=True)
        return None


def server_version(ssh: Any) -> str:
    """The Dango version installed on the server (this client's, if it cannot be read)."""
    from dango import __version__
    from dango.platform.cloud import runtime_ownership as ro
    from dango.platform.cloud.server_status import _get_dango_version

    return ro.valid_version(_get_dango_version(ssh)) or str(__version__)


# ---------------------------------------------------------------------------
# Running steps
# ---------------------------------------------------------------------------


def _run(ssh: Any, script: str, timeout: int) -> Any:
    return ssh.exec_command(f"bash -c {shlex.quote(script)}", timeout=timeout)


def _detail(result: Any) -> str:
    return str((result.stderr or result.stdout or "").strip()) or f"exit {result.exit_code}"


def _fallback_text(reason: str) -> str:
    return (
        f"The compose helper could not be enabled ({reason}). Docker access stays on the docker "
        f"group, which is less isolated. {_RETRY_HINT}"
    )


def install_helper(ssh: Any, warnings: list[str]) -> bool:
    """H1 pre-checks, H2 install (helper, sudoers, directories), H3 self-test."""
    reason = _install_helper(ssh)
    if reason is not None:
        warnings.append(_fallback_text(reason))
    return reason is None


def _install_helper(ssh: Any) -> str | None:
    from dango.platform.cloud import runtime_ownership as ro

    if ro.venv_is_untrusted(ssh):
        return (
            "the server venv can be written by a non-root user; `dango remote upgrade` rebuilds it"
        )
    result = _run(ssh, prechecks_script(), 120)
    if not result.success:
        return _detail(result)
    result = _run(ssh, "# step:H2\n" + ch.install_script(), 120)
    if not result.success:
        return _detail(result)
    result = _run(ssh, self_test_script(), 240)
    if not result.success:
        return _detail(result)
    return None


def _project_id(ssh: Any) -> str | None:
    import yaml

    from dango.platform.cloud.project_files import read_project_file

    try:
        text = read_project_file(ssh, f"{PROJECT_DIR}/.dango/project.yml")
        data = yaml.safe_load(text) if text.strip() else None
    except Exception:
        return None
    project = data.get("project") if isinstance(data, dict) else None
    value = project.get("id") if isinstance(project, dict) else None
    return value if isinstance(value, str) and ID_RE.fullmatch(value) else None


def _pin_and_render(ssh: Any, warnings: list[str]) -> str | None:
    """P1 consistency check and pin, P2 render, P3 verify; the failure reason, or None."""
    project_id = _project_id(ssh)
    if project_id is None:
        return "the project id could not be read from project.yml"
    result = _run(ssh, consistency_script(project_id), 180)
    if not result.success:
        return _detail(result)
    pinned = ""
    for line in result.stdout.splitlines():
        if line.startswith("WARN:"):
            warnings.append(line[len("WARN:") :])
        elif line.startswith("PINNED="):
            pinned = line[len("PINNED=") :]
    if pinned.startswith("other"):
        return f"the compose project is already pinned to another id ({pinned.partition(':')[2]})"
    if pinned == "none":
        pin = ssh.exec_command(f"{ch.HELPER_PATH} pin-project {project_id}", timeout=120)
        if not pin.success:
            return _detail(pin)
    for script, timeout in ((render_script(), RENDER_TIMEOUT), (verify_script(), 120)):
        result = _run(ssh, script, timeout)
        if not result.success:
            return _detail(result)
    return None


def pin_and_render(ssh: Any, warnings: list[str]) -> bool:
    """Pin the project name, render the server compose file and verify the helper can use it."""
    reason = _pin_and_render(ssh, warnings)
    if reason is not None:
        warnings.append(_fallback_text(reason))
    return reason is None


def enter_fallback(ssh: Any, warnings: list[str], version: str, reason: str) -> None:
    """Group access again: no marker, the account in the docker group, ``group`` recorded."""
    result = _run(ssh, fallback_script(version), 60)
    if not result.success:
        warnings.append(
            f"Docker group access could not be restored automatically ({_detail(result)}). "
            "Run `usermod -aG docker dango` on the server and remove the marker "
            f"{ch.MARKER} if it exists."
        )
    warnings.append(_fallback_text(reason))


def _write_record(ssh: Any, version: str, access: str) -> None:
    result = _run(ssh, record_script(version, access), 60)
    if not result.success:
        _logger.warning("server_version_record_failed", detail=_detail(result))


def rebuild_command(compose_project: str) -> str:
    """Pull and rebuild Metabase: through the helper when it is in use, else the legacy commands."""
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


def _rebuild(ssh: Any, warnings: list[str]) -> None:
    from dango.platform.cloud.backup import get_remote_compose_project_name, helper_branch_active

    project = get_remote_compose_project_name(ssh, PROJECT_DIR)
    result = ssh.exec_command(
        rebuild_command(project), timeout=rebuild_timeout(helper_branch_active(ssh))
    )
    if not result.success:
        warnings.append("Docker rebuild did not complete successfully.")


def needs_migration(ssh: Any) -> bool:
    """False only when the server is settled on the helper (marker, no group, not mid-switch)."""
    from dango.platform.cloud.server_status import _get_docker_access

    access, _recorded = _get_docker_access(ssh)
    return access != ACCESS_HELPER


def _web(ssh: Any, action: str) -> None:
    result = ssh.exec_command(f"systemctl {action} dango-web", timeout=120)
    if not result.success:
        _logger.warning("dango_web_service_failed", action=action, detail=_detail(result))


def migrate_docker_access(
    ssh: Any,
    warnings: list[str],
    *,
    version: str,
    on_step: Callable[[], None] | None = None,
    manage_web: bool = False,
    rebuild: bool = False,
) -> str:
    """Switch to the helper; returns ``helper``, ``migrating`` or ``group`` (the fallback).

    With *manage_web*, dango-web is stopped right before the group is removed and started again at
    the end; *rebuild* recreates Metabase through the helper before that start.  Steps before the
    marker never touch dango-web.
    """
    tick = on_step or (lambda: None)
    reason = _install_helper(ssh)
    tick()
    if reason is None:
        reason = _pin_and_render(ssh, warnings)
        tick()
    if reason is None:
        result = _run(ssh, marker_script(version, ACCESS_MIGRATING), 60)
        reason = None if result.success else _detail(result)
        tick()
    if reason is not None:
        enter_fallback(ssh, warnings, version, reason)
        return ACCESS_GROUP
    if manage_web:
        _web(ssh, "stop")
    try:
        state = _switch_group(ssh, warnings, version, tick)
        if rebuild:
            _rebuild(ssh, warnings)
            tick()
    finally:
        if manage_web:
            _web(ssh, "start")
    return state


def _switch_group(ssh: Any, warnings: list[str], version: str, tick: Callable[[], None]) -> str:
    result = _run(ssh, drop_group_script(), 60)
    tick()
    if not result.success:
        warnings.append(
            "The service account is still in the docker group "
            f"({_detail(result)}); both access paths work. {_RETRY_HINT}"
        )
        return ACCESS_MIGRATING
    result = _run(ssh, terminate_script(), 420)
    tick()
    if not result.success:
        detail = " ".join(result.stdout.split()) or _detail(result)
        warnings.append(
            "Some processes of the service account started before the change may still hold "
            f"docker group access ({detail}). {_RETRY_HINT}"
        )
        return ACCESS_MIGRATING
    left = [line for line in result.stdout.splitlines() if line.startswith("T_LEFT:")]
    if left:
        warnings.append(
            "Processes of the service account that were still running were ended: "
            + left[0][len("T_LEFT:") :].strip()
            + " Re-run anything that was interrupted."
        )
    _write_record(ssh, version, ACCESS_HELPER)
    return ACCESS_HELPER


def enable_fresh(ssh: Any, warnings: list[str], version: str) -> str:
    """Fresh server: install and verify the helper, then write the marker (no project yet)."""
    reason = _install_helper(ssh)
    if reason is None:
        result = _run(ssh, marker_script(version, ACCESS_HELPER), 60)
        reason = None if result.success else _detail(result)
    if reason is not None:
        enter_fallback(ssh, warnings, version, reason)
        return ACCESS_GROUP
    return ACCESS_HELPER


def prebuild_images(ssh: Any, warnings: list[str], *, legacy_command: str) -> None:
    """First deploy: pin, render and build through the helper, else fall back to the group.

    A refusal by the helper (or a pin/render failure) falls back with a loud warning and builds the
    legacy way; an ordinary build failure raises.
    """
    from dango.platform.cloud.backup import helper_branch_active, root_helper_timeout

    if helper_branch_active(ssh):
        reason = _pin_and_render(ssh, warnings)
        if reason is None:
            result = ssh.exec_command(
                f"{ch.HELPER_PATH} build", timeout=root_helper_timeout("build")
            )
            if result.success:
                return
            if result.exit_code not in HELPER_REFUSALS:
                raise CloudProvisioningError(f"Docker image build failed:\n{_detail(result)}")
            reason = f"the helper refused the build: {_detail(result)}"
        enter_fallback(ssh, warnings, server_version(ssh), reason)
    result = ssh.exec_command(legacy_command, timeout=900)
    if not result.success:
        raise CloudProvisioningError(f"Docker image build failed:\n{_detail(result)}")


def ensure_pinned(ssh: Any, warnings: list[str]) -> None:
    """After a restore onto a fresh helper server: pin and render, else fall back to the group.

    Does nothing when the helper is not in use or the project name is already pinned.
    """
    from dango.platform.cloud.backup import helper_branch_active

    if not helper_branch_active(ssh):
        return
    pinned = f"test -s {ch.COMPOSE_DIR}/project-id && test -s {ch.COMPOSE_DIR}/project"
    if ssh.exec_command(pinned, timeout=30).success:
        return
    reason = _pin_and_render(ssh, warnings)
    if reason is not None:
        enter_fallback(ssh, warnings, server_version(ssh), reason)


def restore_docker_access(ssh: Any, warnings: list[str], *, version: str) -> None:
    """Downgrade below the helper release: group access back, volume ownership back, helper off."""
    result = _run(ssh, restore_script(version), 600)
    if not result.success:
        raise CloudProvisioningError(f"Could not restore Docker group access: {_detail(result)}")
    for line in result.stdout.splitlines():
        if line.startswith("RESTORE_WARN:"):
            warnings.append(line[len("RESTORE_WARN:") :])


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
