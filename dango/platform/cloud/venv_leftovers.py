"""dango/platform/cloud/venv_leftovers.py

Best-effort steps around the server venv: the optional spaCy model and a warning about leftovers.

Neither step may abort a deploy or an upgrade: a failure becomes a warning that names the
exact command to run by hand.  Dango never deletes a moved-aside venv itself: it only names
the leftovers and the command an operator runs as root after checking.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from rich.markup import escape

from dango.exceptions import CloudError
from dango.platform.cloud.runtime_ownership import ROOT_PYTHON, SRV_DIR, VENV_DIR

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

SPACY_MODEL = "en_core_web_sm"
#: Name of a venv a rebuild moved aside (``<venv>.untrusted-<hex>``).
STALE_VENV_GLOB = "venv.untrusted-*"
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


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
#: The name a rebuild gives the venv it moves aside (``venv.untrusted-`` + 12 hex digits).
_OUR_NAME = re.compile(r"venv\.untrusted-[0-9a-f]{12}")
_MAX_NAME = 120


def _shown(name: str) -> str:
    """*name* from the server made safe to print: control characters dropped, markup escaped."""
    return escape(_CONTROL.sub("", name)[:_MAX_NAME])


def warn_stale_venvs(ssh: SSHManager, warnings: list[str]) -> None:
    """Add a warning naming ``venv.untrusted-*`` entries in ``/srv/dango`` (read-only).

    An interrupted rebuild leaves its moved-aside venv there. Dango does not remove it: the
    names come from a directory the service account could once write. For each name Dango
    itself would have chosen the warning gives the exact ``rm -rf`` (no glob) for an operator
    to run as root after checking that no upgrade is running; any other name is only listed.
    Never raises.
    """
    try:
        result = ssh.exec_command(
            f"find {SRV_DIR} -maxdepth 1 -name {shlex.quote(STALE_VENV_GLOB)} -print0 2>/dev/null",
            timeout=60,
        )
    except CloudError:
        return
    prefix = f"{SRV_DIR}/"
    names = [p[len(prefix) :] for p in result.stdout.split("\0") if p.startswith(prefix)]
    if not names:
        return
    ours = sorted(n for n in names if _OUR_NAME.fullmatch(n))
    other = sorted(n for n in names if not _OUR_NAME.fullmatch(n))
    parts = [
        f"Old venv(s) found in {SRV_DIR}: "
        + ", ".join(_shown(n) for n in sorted(names))
        + ". They come from an interrupted rebuild (or from an upgrade that is still running) "
        "and Dango does not remove them."
    ]
    if ours:
        parts.append(
            "Once you have checked that no upgrade is running and that you do not need them, "
            "remove each as root: " + "; ".join(f"rm -rf {SRV_DIR}/{n}" for n in ours) + "."
        )
    if other:
        parts.append("Inspect the entries with other names by hand before removing them.")
    warnings.append(" ".join(parts))
