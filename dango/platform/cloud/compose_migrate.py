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
    member_script,
    ownership_steps,
    prechecks_script,
    record_script,
    render_script,
    restore_script,
    self_test_script,
    terminate_script,
    verify_script,
)
from dango.platform.cloud.compose_steps import (
    LEAVE_TIMEOUT,
    detail,
    rebuild_command,
    rebuild_metabase,
    rebuild_pending,
    rebuild_timeout,
    refresh_web_unit,
    relay_warnings,
    retry_pending_rebuild,
    run_script,
    upgrade_rebuild,
    web_service,
    write_record,
)
from dango.platform.cloud.compose_versions import (
    below_helper_version,
    client_upgrade_line,
    client_version_notice,
    helper_capable,
    server_version,
)
from dango.platform.compose_exec import EXIT_PIN

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
    "MetabasePinRequired",
    "needs_restore",
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
    "rebuild_pending",
    "refresh_web_unit",
    "restore_docker_access",
    "restore_script",
    "retry_pending_rebuild",
    "self_test_script",
    "server_version",
    "terminate_script",
    "upgrade_rebuild",
]

_logger = get_logger(__name__)


class MetabasePinRequired(CloudProvisioningError):
    """The helper refused the Metabase jar pin (render exit 68) before anything was changed.

    Never answered by the group fallback or a legacy build: an administrator pins the digest.
    """


_PIN_CMD = f"`{ch.HELPER_PATH} pin-metabase VERSION SHA256`"
#: Recovery text per caller: only the caller knows which retry actually builds the image.
PIN_RECOVERY_UPGRADE = f"As root on the server run {_PIN_CMD}, then re-run `dango remote upgrade`."
PIN_RECOVERY_NEW_SERVER = (
    "A custom Metabase version needs an administrator pin on an existing server: deploy with the "
    "default Metabase version (set MB_VERSION in Dockerfile.metabase to the default for that "
    f"deploy), then as root on the server run {_PIN_CMD}, set the custom version in "
    "Dockerfile.metabase again and run `dango remote push`."
)


#: Helper exits that mean "the helper refused or is unusable", as opposed to an ordinary failure.
HELPER_REFUSALS = frozenset({64, 65, 66, 67, 75})
#: SSH timeout for the render script (helper render budget + sudo lock wait + margin).
RENDER_TIMEOUT = ch.SUDO_LOCK_WAIT + ch.VERB_BUDGET["render"] + 2 * ch.CALLER_MARGIN

_RETRY_HINT = "Run `dango remote upgrade` to retry once the cause is fixed."
#: The failed deploy already saved .dango/cloud.yml (a DigitalOcean failure removes it again), and
#: `dango deploy` refuses while it exists.
_DEPLOY_RETRY = (
    "Fix the cause, delete .dango/cloud.yml on this machine if it exists, then run "
    "`dango deploy` again."
)
PIN_RECOVERY_BYOS = (
    f"As root on the server run {_PIN_CMD} if the pin is missing, then delete .dango/cloud.yml "
    "on this machine and run `dango deploy` again."
)


# ---------------------------------------------------------------------------
# Running steps
# ---------------------------------------------------------------------------


#: Start of the loud fallback warning (the deploy carries lines starting with it to its end).
FALLBACK_MARK = "The compose helper could not be enabled"


def _fallback_text(reason: str) -> str:
    return (
        f"{FALLBACK_MARK} ({reason}). Docker access stays on the docker "
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
    result = run_script(ssh, prechecks_script(), 120)
    if not result.success:
        return detail(result)
    result = run_script(ssh, "# step:H2\n" + ch.install_script(), 120)
    if not result.success:
        return detail(result)
    result = run_script(ssh, self_test_script(), 240)
    if not result.success:
        return detail(result)
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


def _pin_and_render(
    ssh: Any, warnings: list[str], recovery: str = PIN_RECOVERY_UPGRADE
) -> str | None:
    """P1 consistency check and pin, P2 render, P3 verify; the failure reason, or None."""
    project_id = _project_id(ssh)
    if project_id is None:
        return "the project id could not be read from project.yml"
    result = run_script(ssh, consistency_script(project_id), 180)
    if not result.success:
        return detail(result)
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
            return detail(pin)
    result = run_script(ssh, render_script(), RENDER_TIMEOUT)
    if result.exit_code == EXIT_PIN:
        raise MetabasePinRequired(f"{detail(result)}. {recovery}")
    if not result.success:
        return detail(result)
    result = run_script(ssh, verify_script(), 120)
    return None if result.success else detail(result)


def pin_and_render(ssh: Any, warnings: list[str]) -> bool:
    """Pin the project name, render the server compose file and verify the helper can use it."""
    reason = _pin_and_render(ssh, warnings)
    if reason is not None:
        warnings.append(_fallback_text(reason))
    return reason is None


def enter_fallback(
    ssh: Any,
    warnings: list[str],
    version: str,
    reason: str,
    *,
    switched: bool,
    own_marker: bool = False,
) -> str:
    """Group access again, only when the helper was never settled; returns the access state.

    *switched* is whether the trusted marker existed before this command started.  Then a failure
    keeps the switch (marker kept, no ``usermod``): the failing inputs belong to the service
    account.  Only a deploy whose own ``enable_fresh`` created the marker (*own_marker*) may fall
    back: marker removed, account in the ``docker`` group, ``group`` recorded.
    """
    if switched and not own_marker:
        return _keep_switched(ssh, warnings, reason)
    result = run_script(ssh, fallback_script(version), LEAVE_TIMEOUT)
    if not result.success:
        warnings.append(
            f"Docker group access could not be restored automatically ({detail(result)}). "
            "Run `usermod -aG docker dango` on the server and remove the marker "
            f"{ch.MARKER} if it exists."
        )
    relay_warnings(result, warnings)
    warnings.append(_fallback_text(reason))
    return ACCESS_GROUP


def needs_migration(ssh: Any) -> bool:
    """False only when the server is settled on the helper (marker, no group, not mid-switch)."""
    from dango.platform.cloud.server_status import _get_docker_access

    access, _recorded = _get_docker_access(ssh)
    return access != ACCESS_HELPER


def needs_restore(ssh: Any) -> bool:
    """True when the server still runs through the helper, has no Docker access, or cannot be read.

    Decided from the server's state, so an interrupted downgrade is finished by the next run.
    """
    from dango.platform.cloud.server_status import _get_docker_access

    access, _recorded = _get_docker_access(ssh)
    if access == "group (fallback)":
        # the group came back through the fallback; a helper that ran left its sudoers rule
        return bool(ssh.exec_command(f"test -e {ch.SUDOERS_PATH}", timeout=30).success)
    # an unreadable probe restores too: the script is idempotent
    return access in (ACCESS_HELPER, ACCESS_MIGRATING, "none", "unknown")


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
    from dango.platform.cloud.backup import helper_branch_active

    tick = on_step or (lambda: None)
    switched = helper_branch_active(ssh)
    reason = _install_helper(ssh)
    tick()
    if reason is None:
        reason = _pin_and_render(ssh, warnings)
        tick()
    if reason is None:
        result = run_script(ssh, marker_script(version, ACCESS_MIGRATING), 60)
        reason = None if result.success else detail(result)
        tick()
    if reason is not None:
        return enter_fallback(ssh, warnings, version, reason, switched=switched)
    state = ACCESS_MIGRATING
    try:
        if manage_web:
            web_service(ssh, "stop")
            tick()
        state = _switch_group(ssh, warnings, version, tick)
        if rebuild:
            rebuild_metabase(ssh, warnings, require_helper=True)
            tick()
    finally:
        if manage_web:
            web_service(ssh, "start")
    return state


def _keep_switched(ssh: Any, warnings: list[str], reason: str) -> str:
    """A re-run failed on a server that already uses the helper: keep it, warn, never add the group.

    The inputs of the failing steps (project.yml, plugins) are controlled by the service account, so
    root must not answer a failure by giving the account Docker access again.
    """
    warnings.append(
        f"The compose helper could not be refreshed ({reason}). The helper stays enabled (the "
        "marker is kept) and the service account is not added to the docker group. "
        f"{_RETRY_HINT}"
    )
    return ACCESS_MIGRATING if run_script(ssh, member_script(), 30).success else ACCESS_HELPER


def _switch_group(ssh: Any, warnings: list[str], version: str, tick: Callable[[], None]) -> str:
    result = run_script(ssh, drop_group_script(), 60)
    tick()
    if not result.success:
        warnings.append(
            "The service account is still in the docker group "
            f"({detail(result)}); both access paths work. {_RETRY_HINT}"
        )
        return ACCESS_MIGRATING
    result = run_script(ssh, terminate_script(), 420)
    tick()
    if not result.success:
        info = " ".join(result.stdout.split()) or detail(result)
        warnings.append(
            "Some processes of the service account started before the change may still hold "
            f"docker group access ({info}). {_RETRY_HINT}"
        )
        return ACCESS_MIGRATING
    left = [line for line in result.stdout.splitlines() if line.startswith("T_LEFT:")]
    if left:
        warnings.append(
            "Processes of the service account that were still running were ended: "
            + left[0][len("T_LEFT:") :].strip()
            + " Re-run anything that was interrupted."
        )
    write_record(ssh, version, ACCESS_HELPER)
    return ACCESS_HELPER


def enable_fresh(ssh: Any, warnings: list[str], version: str) -> str:
    """Fresh server: install and verify the helper, then write the marker (no project yet).

    A server an older release set up has the service account in the docker group: it is removed.
    A failure on a server whose marker already existed keeps the switch; otherwise it falls back.
    """
    from dango.platform.cloud.backup import helper_branch_active

    switched = helper_branch_active(ssh)
    reason = _install_helper(ssh)
    member = False
    if reason is None:
        member = run_script(ssh, member_script(), 30).success
        access = ACCESS_MIGRATING if member else ACCESS_HELPER
        result = run_script(ssh, marker_script(version, access), 60)
        reason = None if result.success else detail(result)
    if reason is not None:
        return enter_fallback(ssh, warnings, version, reason, switched=switched)
    if member:
        return _switch_group(ssh, warnings, version, lambda: None)
    return ACCESS_HELPER


def prebuild_images(
    ssh: Any,
    warnings: list[str],
    *,
    legacy_command: str | None = None,
    own_marker: bool = False,
    recovery: str = PIN_RECOVERY_BYOS,
) -> None:
    """First deploy: pin, render and build through the trusted helper; there is no other build.

    A missing helper, a refusal, a timeout or any build failure raises, and the server's Docker
    access is left as it was: a failed fresh deploy is not put into the docker-group fallback,
    where dango-web could build Metabase from the project recipe.  Without a rendered, verified
    build directory the helper cannot start Metabase either.  A pin refusal raises
    ``MetabasePinRequired`` with the caller's *recovery* text.  *legacy_command* and *own_marker*
    are unused since 1.0.14 (kept for callers): a project-owned recipe is never built on a server.
    """
    from dango.platform.cloud.backup import helper_branch_active, root_helper_timeout

    if not helper_branch_active(ssh):
        raise CloudProvisioningError(
            "Docker image build did not run: the verified Metabase build needs the compose "
            f"helper, which is not enabled on this server. {_DEPLOY_RETRY}"
        )
    reason = _pin_and_render(ssh, warnings, recovery)
    if reason is None:
        result = ssh.exec_command(f"{ch.HELPER_PATH} build", timeout=root_helper_timeout("build"))
        if result.success:
            return
        if result.exit_code not in HELPER_REFUSALS:
            raise CloudProvisioningError(f"Docker image build failed:\n{detail(result)}")
        reason = f"the helper refused the build: {detail(result)}"
    raise CloudProvisioningError(
        f"Docker image build did not run: the compose helper is not usable ({reason}). "
        f"The helper stays enabled. {_DEPLOY_RETRY}"
    )


def ensure_pinned(ssh: Any, warnings: list[str]) -> None:
    """After a restore onto a fresh helper server: pin and render; a failure keeps the switch.

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
        enter_fallback(ssh, warnings, server_version(ssh), reason, switched=True)


def restore_docker_access(
    ssh: Any,
    warnings: list[str],
    *,
    version: str,
    manage_web: bool = False,
    rebuild: bool = False,
    on_step: Callable[[], None] | None = None,
) -> None:
    """Downgrade below the helper release: group access back, helper containers down, volume
    ownership back, helper off.  With *manage_web*, dango-web is stopped around it (and started
    again, also on failure); *rebuild* recreates Metabase the legacy way before that start."""
    tick = on_step or (lambda: None)
    try:
        if manage_web:
            web_service(ssh, "stop")
            tick()
        result = run_script(ssh, restore_script(version), LEAVE_TIMEOUT)
        tick()
        if not result.success:
            raise CloudProvisioningError(f"Could not restore Docker group access: {detail(result)}")
        relay_warnings(result, warnings)
        if rebuild:
            rebuild_metabase(ssh, warnings)
            tick()
    finally:
        if manage_web:
            web_service(ssh, "start")
