"""dango/platform/cloud/runner_reapply.py

Re-apply the runner-isolation permission steps after something that can recreate the protected
paths: the end of a deploy, a file push, a restore.  Only when isolation is on (the marker
exists).  The steps are the same idempotent ones the setup runs: no runner process may exist
(``dango-web`` is stopped first when it is running), project modes and deny ACLs are set as the
service account, the notebook ACL pass runs, and the verification must pass.  A failure is a
loud warning, never an exception: the data operation that called this has already succeeded.
"""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from dango.logging import get_logger
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as ri
from dango.platform.cloud import runner_scripts as scripts

__all__ = ["REAPPLY_MARK", "reapply", "reapply_local", "reapply_steps"]

_logger = get_logger(__name__)

#: Start of the warning when the permission steps could not be re-applied.
REAPPLY_MARK = "Runner permissions were not re-applied"
_HINT = "Run `dango remote upgrade` to repair them."
_STEP_TIMEOUT = 120


def reapply_steps(srv_dir: str = scripts.SRV_DIR, *, stop_web: bool = False) -> list[ri.Step]:
    """Quiesce, project permissions, notebook ACLs and their report, verify (no marker or users)."""
    plan = ri.build_steps(srv_dir)
    notebook = [s for s in plan if s.name in ("notebooks_acl", "notebooks_report")]
    return [
        ri.Step("quiesce", access.quiesce_script(stop_web=stop_web)),
        ri.Step(
            "project_permissions",
            access.project_permission_script(srv_dir=srv_dir),
            run_as="service",
        ),
        *notebook,
        ri.Step("verify", access.verify_script(srv_dir=srv_dir)),
    ]


def _warning(outcome: ri.Outcome) -> str:
    return f"{REAPPLY_MARK} ({outcome.failed}: {outcome.detail or 'failed'}). {_HINT}"


def reapply(
    ssh: Any,
    warnings: list[str],
    *,
    stop_web: bool = False,
    restart: bool = True,
    srv_dir: str = scripts.SRV_DIR,
) -> str | None:
    """Re-apply over SSH when isolation is on; returns ``None`` when it is not.

    *stop_web* stops ``dango-web`` for the steps (a deploy end, where it is running) and starts
    it again; the push and restore paths have the services stopped already.  With
    ``restart=False`` the caller starts it (a deploy that restarts it next anyway: one start only).
    """
    marker = shlex.quote(scripts.marker_path(srv_dir))
    try:
        present = bool(ssh.exec_command(f"test -f {marker}").success)
    except Exception:
        present = False
    if not present:
        return None
    was_active = stop_web and restart and ri.web_is_active(ssh)
    try:
        outcome = ri.run_steps(reapply_steps(srv_dir, stop_web=stop_web), ri.executor(ssh, None))
    finally:
        if was_active:
            ri.start_web(ssh, warnings)
    if not outcome.enabled:
        warnings.append(_warning(outcome))
        return _warning(outcome)
    warnings.extend(w for w in outcome.warnings if w not in warnings)
    return "Runner permissions re-applied"


def _local_executor() -> Callable[[ri.Step], ri.StepResult]:
    def execute(step: ri.Step) -> ri.StepResult:
        """Run one step with ``bash -c`` on this machine (the caller is root)."""
        try:
            proc = subprocess.run(
                ["bash", "-c", step.command],
                capture_output=True,
                text=True,
                timeout=_STEP_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ri.StepResult(False, type(exc).__name__)
        if proc.returncode == 0:
            return ri.StepResult(True)
        return ri.StepResult(False, ri.failure_detail(proc.stderr, proc.stdout, proc.returncode))

    return execute


def reapply_local(srv_dir: str = scripts.SRV_DIR) -> list[str]:
    """Re-apply from the server itself (a restore run by the backup tool as root).

    The services are stopped by the caller.  Returns the warnings (also logged).
    """
    if not Path(scripts.marker_path(srv_dir)).is_file():
        return []
    outcome = ri.run_steps(reapply_steps(srv_dir), _local_executor())
    if outcome.enabled:
        return list(outcome.warnings)
    text = _warning(outcome)
    _logger.warning("runner_permissions_not_reapplied", detail=text)
    return [text]
