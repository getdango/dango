"""dango/platform/cloud/venv_leftovers.py

Best-effort steps around the server venv: the optional spaCy model and moved-aside old venvs.

Neither step may abort a deploy or an upgrade: a failure becomes a warning that names the
exact command to run by hand.  The sweep only runs when no other upgrade or venv build is
running, so it can never remove the venv a live rebuild moved aside to restore from.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from dango.exceptions import CloudError
from dango.platform.cloud.runtime_ownership import ROOT_PYTHON, SRV_DIR, VENV_DIR
from dango.platform.cloud.upgrade_guard import GUARD_ACTIVE_CMD

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

SPACY_MODEL = "en_core_web_sm"
#: Name of a venv a rebuild moved aside (``<venv>.untrusted-<hex>``).
STALE_VENV_GLOB = "venv.untrusted-*"
#: Exit 0 while a venv build (pip, venv creation, spaCy download) is running on the server.
#: The ``[x]`` brackets keep pgrep from matching the shell that runs this command.
BUILD_RUNNING_CMD = (
    'pgrep -f "[/]srv/dango/venv/bin/[p]ip install" >/dev/null || '
    'pgrep -f "[-]m venv /srv/dango" >/dev/null || '
    'pgrep -f "[-]m spacy download" >/dev/null'
)
#: Seconds before the SSH read timeout at which the server stops the spaCy download itself.
_SPACY_MARGIN = 15


def install_spacy_model(ssh: SSHManager, warnings: list[str], *, timeout: int) -> None:
    """Install the spaCy model used for PII scanning; a timeout or failure is only a warning.

    The model is optional: PII scanning is unavailable until it is installed. A slow link makes
    the SSH command time out (``CloudSSHError``); that must not undo a finished venv build.
    The server stops the download (its whole process group, pip included) before the SSH
    timeout, so nothing is left running when the next root step starts.
    """
    retry = f"{ROOT_PYTHON} -m spacy download {SPACY_MODEL}"
    limit = max(30, timeout - _SPACY_MARGIN)
    run = (
        f"cd / && timeout -k 5 {limit} {VENV_DIR}/bin/python -I -m spacy download {SPACY_MODEL} -q"
    )
    reason = "the download failed"
    try:
        result = ssh.exec_command(run, timeout=timeout)
        if result.success:
            return
        if result.exit_code in (124, 137):
            reason = f"the download did not finish within {limit} seconds"
    except CloudError as exc:
        reason = f"the download did not finish ({type(exc).__name__})"
    warnings.append(
        f"spaCy model '{SPACY_MODEL}' could not be installed ({reason}). "
        f"PII scanning will be unavailable until installed manually: {retry}"
    )


def _server_is_idle(ssh: SSHManager) -> bool:
    """True when no other upgrade guard is active and no venv build is running (else, or if
    that cannot be told, False: the sweep is skipped)."""
    try:
        if ssh.exec_command(GUARD_ACTIVE_CMD, timeout=30).success:
            return False
        return not ssh.exec_command(BUILD_RUNNING_CMD, timeout=30).success
    except CloudError:
        return False


def remove_stale_venvs(ssh: SSHManager, warnings: list[str]) -> None:
    """Remove ``venv.untrusted-*`` directories an interrupted rebuild left in ``/srv/dango``.

    Skipped unless the server is idle (no other upgrade's guard, no venv build running):
    a live rebuild keeps its old venv aside until it is verified. Only real directories owned
    by root directly under ``/srv/dango`` are removed, and only while the current venv has its
    ``dango`` script (otherwise a leftover may be the only copy). ``find`` does not follow
    links; ``rm`` stays on one filesystem and nothing in them is executed. Anything of that
    name that is kept (another owner, a link) is named in a warning. Never raises.
    """
    if not _server_is_idle(ssh):
        return
    pattern = shlex.quote(STALE_VENV_GLOB)
    present = f'V={VENV_DIR}; [ -x "$V/bin/dango" ]'
    try:
        result = ssh.exec_command(
            f"{present} && find {SRV_DIR} -maxdepth 1 -type d -uid 0 "
            f"-name {pattern} -print -exec rm -rf --one-file-system -- {{}} +",
            timeout=300,
        )
        names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        left = ssh.exec_command(
            f"{present} && find {SRV_DIR} -maxdepth 1 -name {pattern} -print", timeout=60
        )
    except CloudError as exc:
        warnings.append(
            f"Could not check {SRV_DIR} for venvs left by an interrupted rebuild "
            f"({type(exc).__name__}); remove any {SRV_DIR}/{STALE_VENV_GLOB} as root."
        )
        return
    if names and result.success:
        warnings.append("Removed a venv left by an interrupted rebuild: " + ", ".join(names) + ".")
    elif names:
        warnings.append(
            "Could not remove a venv left by an interrupted rebuild; as root, run: rm -rf "
            + " ".join(shlex.quote(n) for n in names)
        )
    kept = [ln.strip() for ln in left.stdout.splitlines() if ln.strip() and ln.strip() not in names]
    if kept:
        warnings.append(
            "Left in place (not removed: not a root-owned directory): "
            + ", ".join(kept)
            + ". Inspect them and, once you no longer need them, remove them as root."
        )
