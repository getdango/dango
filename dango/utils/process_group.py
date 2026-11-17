"""dango/utils/process_group.py

Process-group signalling and detach arguments that work on Windows, macOS and Linux.

POSIX has process groups (``os.killpg``/``os.getpgid``/``os.getpgrp``, ``SIGKILL``); Windows has none of
them. Each Unix-only name is looked up with ``getattr`` at call time, so a missing name is a normal
"no group support" answer instead of an ``AttributeError``, and a test can replace it on ``os``.
Callers use :func:`group_signal_available` to choose between group signalling and the psutil tree kill
(:func:`dango.utils.process.kill_process`).
"""

from __future__ import annotations

import os
import signal
from typing import Any

import psutil

__all__ = [
    "detach_kwargs",
    "group_alive",
    "group_signal_available",
    "leads_own_group",
    "sigkill",
    "signal_group",
]


def group_signal_available() -> bool:
    """True when this platform can signal process groups (POSIX)."""
    return all(getattr(os, name, None) is not None for name in ("killpg", "getpgid", "getpgrp"))


def signal_group(pgid: int, sig: int) -> None:
    """Send *sig* to every process of group *pgid* (``os.killpg``; its errors propagate).

    Raises:
        NotImplementedError: If the platform has no process groups.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        raise NotImplementedError("process groups are not available on this platform")
    killpg(pgid, sig)


def group_alive(pgid: int) -> bool:
    """True while any process is left in group *pgid* (EPERM means one exists).

    Without process groups the group leader's pid stands in for the group.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        return bool(psutil.pid_exists(pgid))
    try:
        killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def leads_own_group(pid: int) -> bool:
    """True when *pid* leads a process group of its own that is not ours.

    Always False without process groups. ``ProcessLookupError`` propagates when *pid* is gone.
    """
    getpgid = getattr(os, "getpgid", None)
    getpgrp = getattr(os, "getpgrp", None)
    if getpgid is None or getpgrp is None:
        return False
    pgid = getpgid(pid)
    return bool(pgid == pid and pgid != getpgrp())


def sigkill() -> int:
    """``SIGKILL``, or ``SIGTERM`` where there is no ``SIGKILL`` (Windows)."""
    return int(getattr(signal, "SIGKILL", signal.SIGTERM))


def detach_kwargs() -> dict[str, Any]:
    """``subprocess.Popen`` keyword arguments that detach a child from this session.

    ``start_new_session=True`` on every platform. Windows ignores it (the child is not detached and a
    console close reaches it); ``creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` would
    detach it, but DETACHED_PROCESS interacts with the standard-handle redirection these callers rely
    on and has not been proven on a real Windows run, so it is deliberately not applied here.
    """
    return {"start_new_session": True}
