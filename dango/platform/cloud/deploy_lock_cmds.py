"""dango/platform/cloud/deploy_lock_cmds.py

Shell commands for the server deploy lock, built so root never opens or follows anything
the service user can plant.

On a server still in the 1.0.13 state ``/srv/dango`` is writable by the service user, so a
lock *file* at a predictable name could be a planted link to a FIFO or a device.  The lock is
a root-owned directory (``mkdir`` never opens or follows what is at the name and fails if
anything is there) holding ``lock.json``.  Every command that touches the inside first
enters the directory and checks that it is owned by root (``[ -O . ]``), so the work happens
in a directory only root can write to.  An entry at the lock path that is not such a
directory counts as a held lock.
"""

from __future__ import annotations

import shlex

#: Exit status of :func:`read_cmd` when the lock path holds anything but a root-owned lock directory.
FOREIGN_LOCK_EXIT = 7
_ABSENT_EXIT = 3


def read_cmd(path: str) -> str:
    """Print the lock JSON; exit 3 if there is no lock, 7 if the path is not a valid lock directory."""
    p = shlex.quote(path)
    return (
        f"if [ -L {p} ]; then exit {FOREIGN_LOCK_EXIT}; fi;"
        f" if [ ! -e {p} ]; then exit {_ABSENT_EXIT}; fi;"
        f" cd {p} 2>/dev/null && [ -O . ] && [ -f lock.json ] && [ ! -L lock.json ]"
        f" && exec cat lock.json; exit {FOREIGN_LOCK_EXIT}"
    )


def create_cmd(path: str, lock_json: str) -> str:
    """Create the lock directory (fails if anything exists at *path*) and write the JSON inside it."""
    p = shlex.quote(path)
    return (
        f"mkdir -m 0700 -- {p} 2>/dev/null || exit 1;"
        f" (cd {p} && [ -O . ] && {{ set -C; echo {shlex.quote(lock_json)} > lock.json; }}) 2>/dev/null"
        f" || {{ rm -rf -- {p}; exit 1; }}"
    )


def remove_cmd(path: str) -> str:
    """Remove the lock (a link at *path* is removed itself, never followed)."""
    return f"rm -rf -- {shlex.quote(path)}"


def remove_if_ours_cmd(path: str, lock_json: str) -> str:
    """Remove the lock only if it is a root-owned lock directory still holding *lock_json*."""
    p = shlex.quote(path)
    return (
        f'cd {p} 2>/dev/null && [ -O . ] && [ "$(cat lock.json 2>/dev/null)" = {shlex.quote(lock_json)} ]'
        f" && rm -rf -- {p}"
    )
