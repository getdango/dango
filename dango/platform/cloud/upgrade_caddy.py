"""dango/platform/cloud/upgrade_caddy.py

Caddyfile WebSocket-route migration used by ``remote upgrade``.

Puts ``forward_auth`` in front of the Marimo WebSocket route in place.  Any
problem is a warning, never an abort.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_CADDYFILE = "/etc/caddy/Caddyfile"
_CADDYFILE_CANDIDATE = "/etc/caddy/Caddyfile.dango-new"
_WS_STATUS = "Caddyfile WebSocket route"
# First release with the forward_auth target /api/notebooks/ws-auth.
_WS_AUTH_MIN_RELEASE = (1, 0, 14)
_WS_MANUAL_STEP = (
    "add `forward_auth` to the `/notebooks/marimo/ws` handle, see the 1.0.14 release notes"
)


def ws_handles_authenticated(content: str) -> bool:
    """True when every ``/notebooks/marimo/ws`` handle has ``forward_auth``."""
    import re

    found = False
    for m in re.finditer(r"(?m)^([ \t]*)handle /notebooks/marimo/ws \{", content):
        found = True
        end = content.find(f"\n{m.group(1)}}}", m.end())
        body = content[m.end() : end if end != -1 else len(content)]
        if "forward_auth" not in body:
            return False
    return found


def has_ws_auth_endpoint(installed_version: str | None) -> bool:
    """True when *installed_version* serves ``/api/notebooks/ws-auth`` (1.0.14+)."""
    from packaging.version import InvalidVersion, Version

    if not installed_version:
        return False
    try:
        return Version(installed_version).release >= _WS_AUTH_MIN_RELEASE
    except InvalidVersion:
        return False


def migrate_caddyfile_ws_route(
    ssh: SSHManager, warnings: list[str], installed_version: str | None
) -> str:
    """Put ``forward_auth`` in front of the Marimo WebSocket route in place.

    Only the exact handle written by 1.0.13 and earlier is replaced; every
    other byte of the Caddyfile is kept, so domains, HSTS and hand edits
    survive.  Any problem is a warning, never an abort.

    Returns:
        A status line for :class:`UpgradeResult`.
    """
    from dango.platform.cloud._server_templates import LEGACY_MARIMO_WS_RE, marimo_ws_block

    def _skip(reason: str) -> str:
        warnings.append(f"{_CADDYFILE} not updated ({reason}). Manually {_WS_MANUAL_STEP}.")
        return f"{_WS_STATUS} skipped: {reason}"

    # Only rewrite when the version now installed on the server serves the
    # forward_auth target; otherwise every notebook WebSocket would be refused.
    if not installed_version:
        warnings.append(
            f"{_CADDYFILE} not updated: the installed Dango version could not be read. "
            "Re-run `dango remote upgrade` (it retries this step even at the same version), "
            f"or manually {_WS_MANUAL_STEP}."
        )
        return f"{_WS_STATUS} skipped: installed version unknown"
    if not has_ws_auth_endpoint(installed_version):
        warnings.append(
            f"{_CADDYFILE} not updated: the installed Dango version ({installed_version}) "
            "has no /api/notebooks/ws-auth endpoint (added in 1.0.14). Re-run "
            "`dango remote upgrade` once 1.0.14 or later is installed."
        )
        return f"{_WS_STATUS} skipped: installed version {installed_version}"

    read = ssh.exec_command(f"cat {_CADDYFILE}", timeout=30)
    if not read.success:
        return _skip("could not read the file")
    original = read.stdout

    matches = list(LEGACY_MARIMO_WS_RE.finditer(original))
    if not matches and "handle /notebooks/marimo/ws" not in original:
        return f"{_WS_STATUS}: no migration needed (no direct WebSocket route)"
    if not matches and ws_handles_authenticated(original):
        return f"{_WS_STATUS} already migrated"
    if len(matches) != 1:
        return _skip(
            "the WebSocket handle does not match the expected 1.0.13 form"
            if not matches
            else "the WebSocket handle appears more than once"
        )

    m = matches[0]
    candidate = original[: m.start()] + marimo_ws_block(int(m.group(1))) + original[m.end() :]

    try:
        ssh.write_remote_file(_CADDYFILE_CANDIDATE, candidate, mode=0o644)
        validate = ssh.exec_command(
            f"caddy validate --adapter caddyfile --config {_CADDYFILE_CANDIDATE}", timeout=60
        )
    finally:
        ssh.exec_command(f"rm -f {_CADDYFILE_CANDIDATE}", timeout=30)
    if not validate.success:
        return _skip("caddy validate rejected the updated file")

    from dango.platform.cloud.domain import ensure_caddyfile

    try:
        ensure_caddyfile(ssh, candidate)
    except Exception:
        try:
            ssh.write_remote_file(_CADDYFILE, original, mode=0o644)
        except Exception:
            return _skip(
                "Caddy reload failed and restoring the original file also failed; "
                f"check {_CADDYFILE} and run `systemctl reload-or-restart caddy`"
            )
        reload = ssh.exec_command("systemctl reload-or-restart caddy", timeout=60)
        if reload.success:
            return _skip("Caddy reload failed; the original file was restored and reloaded")
        return _skip(
            "Caddy reload failed; the original file was restored but Caddy did not reload, "
            "run `systemctl reload-or-restart caddy`"
        )
    return f"{_WS_STATUS} migrated"


def run_ws_migration(ssh: SSHManager, warnings: list[str], installed: str | None) -> str:
    """Run :func:`migrate_caddyfile_ws_route`, turning any exception into a warning."""
    try:
        return migrate_caddyfile_ws_route(ssh, warnings, installed)
    except Exception as exc:
        warnings.append(
            f"{_CADDYFILE} not updated ({type(exc).__name__}). Manually {_WS_MANUAL_STEP}."
        )
        return f"{_WS_STATUS} skipped: {type(exc).__name__}"
