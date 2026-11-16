"""dango/platform/cloud/project_files.py

Write and read files inside the server's project directory as the ``dango`` user.

Deploy steps connect as root, while the project directory is owned by ``dango``.
A root write, append, read or ``chown`` on a project path follows a symlink that
the ``dango`` user placed there (for example ``.env -> /etc/shadow``).  These
helpers run every project-file operation as ``dango`` (``runuser -u dango``), so
a symlink can only reach what ``dango`` could reach anyway:

- :func:`write_project_file` uploads the content to a root-private temp file,
  then ``dango`` copies it to a temp file next to the destination and renames it
  over the destination (a rename replaces a symlink instead of writing through it).
- :func:`append_env_line_if_missing` and :func:`read_project_file` append and
  read as ``dango``.

A session that is already the ``dango`` user (``dango remote env ...`` connects as
``dango``) may not call ``runuser`` (only root may), so for such a session the same
scripts run directly and the upload is staged in a private ``mktemp -d`` directory
instead of ``/root``.  Every other session keeps the ``runuser`` form unchanged.
"""

from __future__ import annotations

import secrets
import shlex
from pathlib import Path
from typing import Any

from dango.exceptions import CloudProvisioningError

__all__ = [
    "AS_DANGO",
    "SERVICE_USER",
    "append_env_line_if_missing",
    "read_project_file",
    "write_project_file",
]

SERVICE_USER = "dango"
AS_DANGO = f"runuser -u {SERVICE_USER} --"

# Runs as dango: $1 = destination, $2 = octal mode, stdin = content.
_WRITE_SCRIPT = (
    'umask 077; d=$(dirname "$1"); mkdir -p "$d" || exit 1; '
    'if [ -d "$1" ] && [ ! -L "$1" ]; then '
    'echo "refusing to replace directory $1" >&2; exit 1; fi; '
    't=$(mktemp "$d/.dango-write.XXXXXX") && '
    '{ cat > "$t" && chmod "$2" "$t" && mv -fT "$t" "$1" || { rm -f "$t"; exit 1; }; }'
)

# Runs as dango: $1 = file.  Exit 3 = the path does not exist; any other failure
# is a real read error.
_READ_SCRIPT = '[ -e "$1" -o -L "$1" ] && exec cat "$1"; exit 3'
_ABSENT_EXIT = 3

# Runs as dango: $1 = file, $2 = KEY, $3 = full line.
_APPEND_SCRIPT = 'grep -q "^$2=" "$1" 2>/dev/null || printf "%s\\n" "$3" >> "$1"'


def _is_service_session(ssh: Any) -> bool:
    """True when the SSH session itself logs in as ``dango`` (``runuser`` is root-only).

    The session's own login identity decides, never the provider or the address.
    """
    return getattr(ssh, "username", None) == SERVICE_USER


def _as_dango(ssh: Any) -> str:
    """Command prefix (with trailing space) that runs a command as ``dango``."""
    return "" if _is_service_session(ssh) else f"{AS_DANGO} "


def write_project_file(
    ssh: Any,
    path: str,
    content: str | bytes,
    mode: int = 0o644,
    *,
    source: Path | None = None,
) -> None:
    """Write *content* to *path* as the ``dango`` user, replacing (not following) it.

    Args:
        ssh: Connected ``SSHManager`` (root, or the ``dango`` user itself).
        path: Absolute destination path inside the project directory.
        content: File content (ignored when *source* is given).
        mode: Permission bits for the new file.
        source: Local file to stage with a pipelined SFTP ``put`` instead of
            sending *content* (large files, e.g. the Metabase driver jar).

    Raises:
        CloudProvisioningError: If the copy into place fails.
    """
    q = shlex.quote
    if _is_service_session(ssh):
        _write_as_service_session(ssh, path, content, mode, source)
        return
    staging = f"/root/.dango-upload-{secrets.token_hex(8)}"
    if source is not None:
        ssh.upload_file(source, staging)
        ssh.exec_command(f"chmod 600 {q(staging)}", timeout=30, check=False)
    else:
        ssh.write_remote_file(staging, content, mode=0o600)
    command = (
        f"{AS_DANGO} sh -c {q(_WRITE_SCRIPT)} _ {q(path)} {mode:o} < {q(staging)}; "
        f"rc=$?; rm -f {q(staging)}; exit $rc"
    )
    result = ssh.exec_command(command, timeout=30)
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Failed to write {path}: {(result.stderr or result.stdout or '').strip()}"
        )


def _write_as_service_session(
    ssh: Any, path: str, content: str | bytes, mode: int, source: Path | None
) -> None:
    """:func:`write_project_file` for a session that already is ``dango``.

    The content is staged in a private ``mktemp -d`` directory (0700, owned by
    ``dango``) instead of ``/root``; the same write script then runs directly.
    """
    q = shlex.quote
    made = ssh.exec_command("mktemp -d", timeout=30)
    stage_dir = (made.stdout or "").strip()
    if made.exit_code != 0 or not stage_dir.startswith("/"):
        raise CloudProvisioningError(
            f"Failed to write {path}: could not create a staging directory: "
            f"{(made.stderr or made.stdout or '').strip()}"
        )
    staging = f"{stage_dir}/content"
    try:
        if source is not None:
            ssh.upload_file(source, staging)
            ssh.exec_command(f"chmod 600 {q(staging)}", timeout=30, check=False)
        else:
            ssh.write_remote_file(staging, content, mode=0o600)
    except BaseException:
        ssh.exec_command(f"rm -rf {q(stage_dir)}", timeout=30, check=False)
        raise
    command = (
        f"sh -c {q(_WRITE_SCRIPT)} _ {q(path)} {mode:o} < {q(staging)}; "
        f"rc=$?; rm -rf {q(stage_dir)}; exit $rc"
    )
    result = ssh.exec_command(command, timeout=30)
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Failed to write {path}: {(result.stderr or result.stdout or '').strip()}"
        )


def append_env_line_if_missing(ssh: Any, path: str, key: str, value: str) -> Any:
    """Append ``KEY=value`` to *path* as ``dango`` unless a ``KEY=`` line exists.

    Returns:
        The ``CommandResult`` of the remote command.
    """
    if any(c in f"{key}{value}" for c in "\n\r\0"):
        raise ValueError("env key and value must not contain newline or NUL characters")
    q = shlex.quote
    return ssh.exec_command(
        f"{_as_dango(ssh)}sh -c {q(_APPEND_SCRIPT)} _ {q(path)} {q(key)} {q(f'{key}={value}')}"
    )


def read_project_file(ssh: Any, path: str) -> str:
    """Return the content of *path* read as ``dango`` (empty if the file is absent).

    Raises:
        CloudProvisioningError: If the file exists but cannot be read (permission
            or SSH error); an unreadable file is not the same as an absent one.
    """
    q = shlex.quote
    result = ssh.exec_command(f"{_as_dango(ssh)}sh -c {q(_READ_SCRIPT)} _ {q(path)}")
    if result.exit_code == _ABSENT_EXIT:
        return ""
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Failed to read {path}: {(result.stderr or result.stdout or '').strip()}"
        )
    return str(result.stdout or "")
