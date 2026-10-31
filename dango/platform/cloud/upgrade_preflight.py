"""dango/platform/cloud/upgrade_preflight.py

Checks ``remote upgrade`` makes before it backs up, arms the guard, stops services or installs
anything, so that a refusal changes nothing (like the interpreter check in ``server_python``).

The Metabase jar pin: from 1.0.14 on the server builds Metabase only from a jar whose SHA-256
is known.  The default version has one built in; any other ``MB_VERSION`` in the project's
``Dockerfile.metabase`` needs a root-owned record in ``compose/metabase-jar.pin`` (see
``metabase_pin``).  Without it the render step refuses, late in the upgrade; this check looks
first, with the server's own parsers (``compose_exec.mb_version_of``,
``metabase_pin.parse_pin_record``), so it refuses exactly what the render would.  The project file
is service-writable, so a forged answer can only let an upgrade run into the render's refusal.
"""

from __future__ import annotations

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
_SEPARATOR = "\n---\n"

# One root call.  ``pinhex=`` is the administrator's record as hex bytes (so that a missing final
# newline or a stray blank is seen), read only when the file and the compose directory are
# root-owned and not group/other-writable (the test the server's render applies; ``/srv/dango``
# itself may still be the service account's before the upgrade secures it).  ``dockerfile=`` says
# whether the project file is absent, a regular file or anything else (a link, a FIFO...), as the
# render opens it without following links.  The project Dockerfile comes last so that its text can
# never be taken for one of the first lines.  ``helper=1`` means the helper can pin right now: it
# is installed and the venv has its module.
PROBE_CMD = (
    "ROOT=0; "
    'safe() { [ -n "$(find "$2" -maxdepth 0 -type "$1" -uid "$ROOT" ! -perm /022 '
    '2>/dev/null)" ]; }; '
    f"if [ -x {ch.HELPER_PATH} ] && find {ch.SRV_DIR}/venv/lib -maxdepth 6 "
    "-path '*/site-packages/dango/platform/cloud/metabase_pin.py' -print 2>/dev/null "
    "| grep -q .; then echo helper=1; else echo helper=0; fi; "
    f"if safe d {ch.COMPOSE_DIR} && safe f {PIN_PATH}; then printf pinhex=; "
    f"timeout 5 head -c {_PIN_MAX} {PIN_PATH} 2>/dev/null | od -An -v -tx1 | tr -d ' \\n'; echo; "
    "else echo pinhex=; fi; "
    f"if [ -L {PROJECT_DOCKERFILE} ]; then df=bad; elif [ ! -e {PROJECT_DOCKERFILE} ]; "
    f"then df=absent; elif [ -f {PROJECT_DOCKERFILE} ]; then df=file; else df=bad; fi; "
    "echo dockerfile=$df; "
    "echo ---; "
    f'if [ "$df" = file ]; then timeout 5 dd if={PROJECT_DOCKERFILE} iflag=nofollow '
    f"bs={_DOCKERFILE_MAX} count=1 2>/dev/null; fi; true"
)


def direct_pin_script(srv_dir: str = ch.SRV_DIR) -> str:
    """One-line POSIX ``sh`` that records the pin without trusting anything the service user owns.

    Arguments: ``$1`` the Metabase version, ``$2`` the SHA-256 (upper case is lowered).  Refuses
    a symlink anywhere in the path to the compose directory or at the record, a compose directory
    that is not ours or that others can write, and a record path that is not a regular file; the
    record is written to a fresh ``mktemp`` file in that directory (exclusive create, mode 0600)
    and moved over with ``mv -T``, which never follows a link at the destination.  Same record
    format, owner and mode as ``metabase_pin.pin_custom``.
    """

    def refuse(why: str) -> str:
        """Shell text that prints *why* and stops."""
        return f'{{ echo "refusing: {why}" >&2; exit 1; }}'

    steps = [
        "set -eu",
        "umask 077",
        "v=$1",
        'h=$(printf %s "$2" | tr A-F a-f)',
        f"case $v in v[0-9]*) ;; *) {refuse('bad version')};; esac",
        f"case ${{v#v}} in *[!0-9.]*) {refuse('bad version')};; esac",
        f"[ ${{#v}} -le 20 ] || {refuse('bad version')}",
        f"case $h in *[!0-9a-f]*) {refuse('the digest must be hexadecimal')};; esac",
        f"[ ${{#h}} -eq 64 ] || {refuse('the digest must be 64 characters')}",
        f"s={shlex.quote(srv_dir)}; d=$s/compose; f=$d/metabase-jar.pin",
        f'[ "$(cd "$s" && pwd -P)" = "$s" ] || {refuse("$s has a symlink in its path")}',
        'if [ -L "$d" ]; then ' + refuse("$d is a symlink") + '; elif [ -e "$d" ]; then '
        'if [ -d "$d" ] && [ -O "$d" ]; then case $(ls -ld "$d") in d????-??-*) ;; *) '
        + refuse("$d can be written by others")
        + ";; esac; else "
        + refuse("$d is not a directory of this user")
        + '; fi; else mkdir -m 0755 "$d"; fi',
        'if [ -L "$f" ]; then ' + refuse("$f is a symlink") + "; fi",
        'if [ -e "$f" ] && [ ! -f "$f" ]; then ' + refuse("$f is not a regular file") + "; fi",
        't=$(mktemp "$d/.pin.XXXXXX")',
        'printf "%s %s\\n" "$v" "$h" > "$t" && chmod 0600 "$t" && mv -fT -- "$t" "$f" '
        '|| { rm -f "$t"; exit 1; }',
    ]
    return "; ".join(steps)


def recovery(version: str, *, helper_usable: bool) -> str:
    """The root command that records the pin: the helper's verb, or the safe direct write.

    The helper can pin only once the server runs 1.0.14 code, which this upgrade installs; on an
    older server the record is written directly (:func:`direct_pin_script`).
    """
    if helper_usable:
        return f"{ch.HELPER_PATH} pin-metabase {version} {_DIGEST_PLACEHOLDER}"
    return f"sh -c {shlex.quote(direct_pin_script())} pin {version} {_DIGEST_PLACEHOLDER}"


def _refusal(
    target: str, old_version: str | None, what: str, then: str, command: str = ""
) -> CloudError:
    """The refusal; a root *command* comes last so that it can be copied as one line."""
    current = f"Dango {old_version}" if old_version else "the installed Dango"
    tail = f" Run as root: {command}" if command else ""
    return CloudError(
        f"Upgrade to {target} refused: {what} Services were not stopped and Dango was not "
        f"changed; {current} keeps running. {then} Then run `dango remote upgrade` again.{tail}",
        error_code=PIN_ERROR_CODE,
    )


def check_metabase_pin(
    ssh: SSHManager, target_version: str, old_version: str | None, *, at_target: bool = False
) -> None:
    """Refuse the upgrade when the render would refuse the project's Metabase version or its pin.

    Does nothing for a target before 1.0.14 (no pin there), for the default version, for a server
    already at the target that is settled on the helper (the run renders nothing), and when the
    server's answer cannot be read (the render step then decides, as before).

    Raises:
        CloudError: ``DANGO-D025``; services were not stopped and Dango was not changed.
    """
    from dango.platform.cloud.metabase_pin import (
        DEFAULT_MB_VERSION,
        PinError,
        parse_pin_record,
    )
    from dango.platform.compose_exec import ComposeRenderError, mb_version_of

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
    state = values.get("dockerfile")
    if state == "absent":
        return  # the render uses the packaged default version
    fix = "Fix Dockerfile.metabase on the server (or locally, then `dango remote push`)."
    if state == "bad":
        raise _refusal(
            target_version,
            old_version,
            "Dockerfile.metabase in the project is not a regular file (the render opens it without "
            "following links).",
            fix,
        )
    if state != "file":
        return
    try:
        version = mb_version_of(text.encode("utf-8", errors="surrogateescape"))
    except ComposeRenderError as exc:
        raise _refusal(
            target_version,
            old_version,
            f"{exc}. The project's Dockerfile.metabase needs exactly one line "
            "`ENV MB_VERSION=vN.N.N` (upper-case ENV, no spaces around =).",
            fix,
        ) from exc
    if version == DEFAULT_MB_VERSION:
        return
    try:
        pin = bytes.fromhex(values.get("pinhex", ""))
    except ValueError:
        return  # not a record we can read; the render decides
    try:
        parse_pin_record(pin, version)
        return
    except PinError:
        pass
    command = recovery(version, helper_usable=values.get("helper") == "1")
    raise _refusal(
        target_version,
        old_version,
        f"Metabase {version} (ENV MB_VERSION in the project's Dockerfile.metabase) has no "
        "administrator pin on the server, so its jar cannot be verified.",
        "As root on the server, after checking the SHA-256 of the jar from Metabase's download, "
        "record the pin with the command at the end of this message.",
        command,
    )
