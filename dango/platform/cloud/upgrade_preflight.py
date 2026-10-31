"""dango/platform/cloud/upgrade_preflight.py

Checks ``remote upgrade`` makes before it backs up, arms the guard, stops services or installs
anything, so that a refusal changes nothing (like the interpreter check in ``server_python``).

The Metabase jar pin: from 1.0.14 on the server builds Metabase only from a jar whose SHA-256
is known.  The default version has one built in; any other ``MB_VERSION`` in the project's
``Dockerfile.metabase`` needs a root-owned record in ``compose/metabase-jar.pin`` (see
``metabase_pin``).  Without it the render step refuses, late in the upgrade; this check looks
first.  It is advisory about the project file (the service account can write it): the server's
own render still decides, so a forged answer can only let an upgrade run into that refusal.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from dango.exceptions import CloudError
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud.compose_versions import helper_capable

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

PIN_ERROR_CODE = "DANGO-D025"
PIN_PATH = f"{ch.COMPOSE_DIR}/metabase-jar.pin"
PROJECT_DOCKERFILE = f"{ch.SRV_DIR}/project/Dockerfile.metabase"
_DOCKERFILE_MAX = 64 * 1024
_PIN_MAX = 129
_DIGEST_PLACEHOLDER = "<sha256-of-the-jar>"
_PIN_RECORD = re.compile(r"(v[0-9][0-9.]{0,18}) ([0-9a-f]{64})")
_SEPARATOR = "\n---\n"

# One root call.  ``pin=`` is the administrator's record, read only when the file and the
# compose directory are root-owned and not group/other-writable (the test the server's render
# applies; ``/srv/dango`` itself may still be the service account's before the upgrade secures it).
# The project Dockerfile comes last so that its text can never be taken for one of the first lines.
# ``helper=1`` means the helper can pin right now: it is installed and the venv has its module.
PROBE_CMD = (
    "ROOT=0; "
    'safe() { [ -n "$(find "$2" -maxdepth 0 -type "$1" -uid "$ROOT" ! -perm /022 '
    '2>/dev/null)" ]; }; '
    f"if [ -x {ch.HELPER_PATH} ] && find {ch.SRV_DIR}/venv/lib -maxdepth 6 "
    "-path '*/site-packages/dango/platform/cloud/metabase_pin.py' -print 2>/dev/null "
    "| grep -q .; then echo helper=1; else echo helper=0; fi; "
    f"if safe d {ch.COMPOSE_DIR} && safe f {PIN_PATH}; then printf pin=; "
    f"timeout 5 head -c {_PIN_MAX} {PIN_PATH} 2>/dev/null | tr -d '\\000-\\037'; echo; "
    "else echo pin=; fi; "
    "echo ---; "
    f"[ ! -L {PROJECT_DOCKERFILE} ] && timeout 5 dd if={PROJECT_DOCKERFILE} iflag=nofollow "
    f"bs={_DOCKERFILE_MAX} count=1 2>/dev/null; true"
)


def _project_version(text: str) -> str | None:
    """The one custom ``MB_VERSION`` the project file sets; None when default, absent or unclear."""
    from dango.platform.cloud.metabase_pin import DEFAULT_MB_VERSION
    from dango.platform.compose_exec import ComposeRenderError, _mb_version

    try:
        version = _mb_version(text)
    except ComposeRenderError:
        return None  # the server's render refuses an unclear file; not this check's business
    return None if version in (None, DEFAULT_MB_VERSION) else version


def recovery(version: str, *, helper_usable: bool) -> str:
    """The root command that records the pin: the helper's verb, or the file itself before it exists.

    The helper can pin only once the server runs 1.0.14 code, which this upgrade installs; on an
    older server the record is written directly, in the format and with the owner and mode that
    ``metabase_pin`` requires.
    """
    if helper_usable:
        return f"{ch.HELPER_PATH} pin-metabase {version} {_DIGEST_PLACEHOLDER}"
    return (
        f"install -d -o root -g root -m 0755 {ch.COMPOSE_DIR} && (umask 077 && printf '%s %s\\n' "
        f"{version} {_DIGEST_PLACEHOLDER} > {shlex.quote(PIN_PATH)})"
    )


def check_metabase_pin(
    ssh: SSHManager, target_version: str, old_version: str | None, *, at_target: bool = False
) -> None:
    """Refuse the upgrade when the project's custom Metabase version has no matching pin.

    Does nothing for a target before 1.0.14 (no pin there), for the default version, for a server
    already at the target that is settled on the helper (the run renders nothing), and when the
    server's answer cannot be read (the render step then decides, as before).

    Raises:
        CloudError: ``DANGO-D025``; services were not stopped and Dango was not changed.
    """
    if not helper_capable(target_version):
        return
    if at_target and not cm.needs_migration(ssh):
        return
    result = ssh.exec_command(f"bash -c {shlex.quote(PROBE_CMD)}", timeout=60)
    if not result.success or not isinstance(result.stdout, str):
        return
    head, sep, text = result.stdout.partition(_SEPARATOR)
    if not sep:
        return
    values = dict(line.split("=", 1) for line in head.splitlines() if "=" in line)
    version = _project_version(text)
    if version is None:
        return
    found = _PIN_RECORD.fullmatch(values.get("pin", ""))
    if found is not None and found.group(1) == version:
        return
    current = f"Dango {old_version}" if old_version else "the installed Dango"
    command = recovery(version, helper_usable=values.get("helper") == "1")
    raise CloudError(
        f"Upgrade to {target_version} refused: Metabase {version} (ENV MB_VERSION in the "
        "project's Dockerfile.metabase) has no administrator pin on the server, so its jar cannot "
        f"be verified. Services were not stopped and Dango was not changed; {current} keeps "
        "running. As root on the server, after checking the SHA-256 of the jar from Metabase's "
        f"download, run: {command}  Then run `dango remote upgrade` again.",
        error_code=PIN_ERROR_CODE,
    )
