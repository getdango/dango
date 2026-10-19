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
"""

from __future__ import annotations

import secrets
import shlex
from typing import Any

from dango.exceptions import CloudProvisioningError

__all__ = [
    "AS_DANGO",
    "append_env_line_if_missing",
    "read_project_file",
    "write_project_file",
]

AS_DANGO = "runuser -u dango --"

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


def write_project_file(ssh: Any, path: str, content: str | bytes, mode: int = 0o644) -> None:
    """Write *content* to *path* as the ``dango`` user, replacing (not following) it.

    Args:
        ssh: Connected root ``SSHManager``.
        path: Absolute destination path inside the project directory.
        content: File content.
        mode: Permission bits for the new file.

    Raises:
        CloudProvisioningError: If the copy into place fails.
    """
    staging = f"/root/.dango-upload-{secrets.token_hex(8)}"
    ssh.write_remote_file(staging, content, mode=0o600)
    q = shlex.quote
    command = (
        f"{AS_DANGO} sh -c {q(_WRITE_SCRIPT)} _ {q(path)} {mode:o} < {q(staging)}; "
        f"rc=$?; rm -f {q(staging)}; exit $rc"
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
    q = shlex.quote
    return ssh.exec_command(
        f"{AS_DANGO} sh -c {q(_APPEND_SCRIPT)} _ {q(path)} {q(key)} {q(f'{key}={value}')}"
    )


def read_project_file(ssh: Any, path: str) -> str:
    """Return the content of *path* read as ``dango`` (empty if the file is absent).

    Raises:
        CloudProvisioningError: If the file exists but cannot be read (permission
            or SSH error); an unreadable file is not the same as an absent one.
    """
    q = shlex.quote
    result = ssh.exec_command(f"{AS_DANGO} sh -c {q(_READ_SCRIPT)} _ {q(path)}")
    if result.exit_code == _ABSENT_EXIT:
        return ""
    if result.exit_code != 0:
        raise CloudProvisioningError(
            f"Failed to read {path}: {(result.stderr or result.stdout or '').strip()}"
        )
    return str(result.stdout or "")
