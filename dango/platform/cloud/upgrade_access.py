"""dango/platform/cloud/upgrade_access.py

``remote upgrade`` at the target version: finish the switch to the compose helper when it is not
settled yet (split out of ``upgrade.py`` to keep that module within the size limit).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dango.platform.cloud import compose_migrate as cm

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager
    from dango.platform.cloud.upgrade_guard import UpgradeGuard


def switch_at_target(
    ssh: SSHManager, warnings: list[str], version: str | None, guard: UpgradeGuard
) -> str | None:
    """At the target version: finish the switch to the compose helper if it is not settled.

    Only a server that is not yet settled on the helper has dango-web stopped (for the group
    removal and process drop) and Metabase recreated; a settled server is not touched.
    """
    if cm.below_helper_version(version):
        # An interrupted downgrade left the helper on under the old code: finish it.
        if not cm.needs_restore(ssh):
            return None
        guard.arm()
        cm.restore_docker_access(
            ssh,
            warnings,
            version=version or "",
            manage_web=True,
            rebuild=True,
            on_step=guard.rearm,
        )
        guard.disarm()  # dango-web was started again; an exception leaves the guard armed
        return cm.ACCESS_GROUP
    if not cm.helper_capable(version):
        return None
    cm.refresh_web_unit(ssh, warnings)  # takes effect at the next restart; never stops dango-web
    if not cm.needs_migration(ssh):
        if cm.rebuild_pending(ssh):  # the last strict rebuild failed: repeat it
            guard.arm()
            cm.retry_pending_rebuild(ssh, warnings)
            guard.disarm()
        return None
    guard.arm()  # dango-web is stopped inside the migration
    try:
        state = cm.migrate_docker_access(
            ssh,
            warnings,
            version=version or "",
            on_step=guard.rearm,
            manage_web=True,
            rebuild=True,
        )
    except cm.MetabasePinRequired:
        guard.disarm()  # refused before dango-web was stopped: nothing needs recovering
        raise
    guard.disarm()  # dango-web was started again; an exception leaves the guard armed
    return state
