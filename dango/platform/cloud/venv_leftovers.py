"""dango/platform/cloud/venv_leftovers.py

Best-effort steps around the server venv: the optional spaCy model and moved-aside old venvs.

Neither step may abort a deploy or an upgrade: a failure becomes a warning that names the
exact command to run by hand.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from dango.exceptions import CloudError
from dango.platform.cloud.runtime_ownership import ROOT_PYTHON, SRV_DIR, VENV_DIR

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

SPACY_MODEL = "en_core_web_sm"
#: Name of a venv a rebuild moved aside (``<venv>.untrusted-<hex>``).
STALE_VENV_GLOB = "venv.untrusted-*"


def install_spacy_model(ssh: SSHManager, warnings: list[str], *, timeout: int) -> None:
    """Install the spaCy model used for PII scanning; a timeout or failure is only a warning.

    The model is optional: PII scanning is unavailable until it is installed. A slow link makes
    the SSH command time out (``CloudSSHError``); that must not undo a finished venv build.
    """
    command = f"{ROOT_PYTHON} -m spacy download {SPACY_MODEL}"
    reason = "the download failed"
    try:
        if ssh.exec_command(f"{command} -q", timeout=timeout).success:
            return
    except CloudError as exc:
        reason = f"the download did not finish ({type(exc).__name__})"
    warnings.append(
        f"spaCy model '{SPACY_MODEL}' could not be installed ({reason}). "
        "PII scanning will be unavailable until installed manually: "
        f"{command}"
    )


def remove_stale_venvs(ssh: SSHManager, warnings: list[str]) -> None:
    """Remove ``venv.untrusted-*`` directories an interrupted rebuild left in ``/srv/dango``.

    Only real directories owned by root directly under ``/srv/dango`` are removed, and only
    while the current venv is present (otherwise a leftover may be the only copy). ``find``
    does not follow links; ``rm`` stays on one filesystem and nothing in them is executed.
    Never raises: a failure is a warning.
    """
    pattern = shlex.quote(STALE_VENV_GLOB)
    try:
        result = ssh.exec_command(
            f'V={VENV_DIR}; [ -x "$V/bin/dango" ] && find {SRV_DIR} -maxdepth 1 -type d -uid 0 '
            f"-name {pattern} -print -exec rm -rf --one-file-system -- {{}} +",
            timeout=300,
        )
    except CloudError as exc:
        warnings.append(
            f"Could not check {SRV_DIR} for venvs left by an interrupted rebuild "
            f"({type(exc).__name__}); remove any {SRV_DIR}/{STALE_VENV_GLOB} as root."
        )
        return
    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        return
    if result.success:
        warnings.append("Removed a venv left by an interrupted rebuild: " + ", ".join(names) + ".")
    else:
        warnings.append(
            "Could not remove a venv left by an interrupted rebuild; as root, run: rm -rf "
            + " ".join(shlex.quote(n) for n in names)
        )
