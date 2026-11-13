"""dango/platform/run_as.py

Single dango-side entry point for running user code as a restricted OS user.

On a server where the root-owned runner marker is present, user code is started as
``sudo -n -u <runner> ... python -I -m dango.runtime.isolated_run ...`` with an
environment built from nothing; otherwise the caller's legacy argv and environment
are returned unchanged.  Nothing in this repository writes the marker or calls this
module yet, so every isolated path is inert.

Kept out of ``platform/common`` (whose package import loads the whole startup
code) for the same reason as :mod:`dango.platform.compose_exec`.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dango.exceptions import DangoError

__all__ = [
    "DISABLED_MARKER",
    "ISOLATION_ERROR_CODE",
    "ISOLATION_MISSING_MESSAGE",
    "MARKER",
    "IsolationRequiredError",
    "RunnerLaunch",
    "assert_distinct_uids",
    "assert_runner_uid",
    "build_command",
    "build_env",
    "build_launch",
    "isolation_state",
    "require_isolation_ready",
    "resolve_declared",
    "runner_pids",
    "runner_uid",
    "runner_user",
    "stop_all",
    "stop_group",
    "uses_isolation",
]

MARKER = "/srv/dango/runner/active"
#: Root-owned opt-out marker (``dango remote isolation disable``); it wins over ``MARKER``.
DISABLED_MARKER = "/srv/dango/runner/disabled"
ISOLATION_ERROR_CODE = "DANGO-ISO001"
ISOLATION_MISSING_MESSAGE = (
    "Script and notebook isolation is not set up on this server. "
    "Run `dango remote upgrade` to enable it "
    "(an admin can opt out with `dango remote isolation disable`)."
)
IsolationState = Literal["local", "on", "disabled", "missing"]
PYTHON = "/srv/dango/venv/bin/python"
SUDO = "/usr/bin/sudo"
KILL = "/usr/bin/kill"
_ROOT_UID = 0

#: role -> (OS user, home and ``DANGO_WORKSPACE``).
_ROLES: dict[str, tuple[str, str]] = {
    "script": ("dango-run", "/srv/dango/runner/scripts"),
    "nb": ("dango-nb", "/srv/dango/runner/nb-home"),
}
#: Uids a runner must never get: Metabase container 999, helper-pinned 61000/61001.
RESERVED_UIDS = frozenset({999, 61000, 61001})
_UID_MIN = 1000
_UID_MAX = 60000  # Debian ``UID_MAX``; ``useradd`` allocates below it.

_BASE_PATH = "/srv/dango/venv/bin:/usr/bin:/bin"
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_POLL = 0.1


@dataclass(frozen=True)
class RunnerLaunch:
    """What to pass to ``Popen``: argv, environment, and whether it is isolated."""

    argv: list[str]
    env: dict[str, str]
    isolated: bool
    #: Declared names that had no value in ``.env`` (reported, never set).
    missing: tuple[str, ...] = ()


def runner_user(role: str) -> str:
    """OS user for *role* (``script`` or ``nb``)."""
    try:
        return _ROLES[role][0]
    except KeyError:
        raise ValueError(f"unknown runner role: {role!r}") from None


class IsolationRequiredError(DangoError):
    """User code was refused because a cloud server has no isolation set up (D2)."""

    _default_error_code = ISOLATION_ERROR_CODE

    def __init__(self) -> None:
        super().__init__(ISOLATION_MISSING_MESSAGE)


def _closed_dir(path: str) -> bool:
    """True when *path* is a real directory owned by root that nobody else can write."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == _ROOT_UID
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _marker_valid(path: str) -> bool:
    """True when *path* is a root-owned regular file nobody else can write (no symlinks).

    The two directories above it (``/srv/dango/runner`` and ``/srv/dango``) must be closed
    root-owned directories too, or a service account could swap one for a link.
    """
    parent = os.path.dirname(path)
    if not (_closed_dir(parent) and _closed_dir(os.path.dirname(parent))):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == _ROOT_UID
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def isolation_state() -> IsolationState:
    """How user code is run on this host.

    The root-owned markers decide first, so a process without ``DANGO_CLOUD_MODE`` cannot
    run user code unisolated on a server that has them.  ``disabled``: a valid opt-out marker
    (an admin's explicit opt-out wins over ``on``).  ``on``: a valid runner marker.  With
    neither, ``missing`` on a cloud server (user code must be refused) and ``local`` elsewhere.
    """
    from dango.config.helpers import is_running_on_cloud

    if _marker_valid(DISABLED_MARKER):
        return "disabled"
    if _marker_valid(MARKER):
        return "on"
    return "missing" if is_running_on_cloud() else "local"


def require_isolation_ready() -> IsolationState:
    """Return the state, or raise :class:`IsolationRequiredError` when it is ``missing``."""
    if uses_isolation():
        return "on"
    state = isolation_state()
    if state == "missing":
        raise IsolationRequiredError
    return state


def uses_isolation() -> bool:
    """True when user code runs as a restricted user (state ``on``)."""
    return isolation_state() == "on"


def _read_env_file(project_root: Path) -> str:
    """Content of ``.env`` read without following a symlink (empty if unreadable)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(project_root / ".env", flags)
    except OSError:
        return ""
    with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def resolve_declared(
    project_root: Path, names: Sequence[str], *, env_text: str | None = None
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Values for the declared *names* from ``.env`` (never ``os.environ``).

    The launcher runs on the server as ``dango`` and reads ``.env`` directly; the
    SSH-side ``project_files.read_project_file`` is for the deploy client.  Pass
    *env_text* to supply the file content from another reader.

    Returns:
        ``(values, missing)``: names with a value, and declared names without one.

    Raises:
        ValueError: If a name is invalid or reserved.
    """
    import io

    from dotenv import dotenv_values

    from dango.config.scripts import check_env_name

    for name in names:
        check_env_name(name)
    text = _read_env_file(project_root) if env_text is None else env_text
    parsed = dotenv_values(stream=io.StringIO(text))
    values = {n: v for n in names if (v := parsed.get(n)) is not None}
    return values, tuple(n for n in names if n not in values)


def build_env(
    role: str,
    project_root: Path,
    declared: Mapping[str, str],
    *,
    run_id: str | None = None,
    tz: str | None = None,
) -> dict[str, str]:
    """Environment for a runner process, built from nothing plus the declared values.

    The notebook role never receives declared values.
    """
    runner_user(role)
    home = _ROLES[role][1]
    env = {
        "PATH": _BASE_PATH,
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DANGO_PROJECT_ROOT": str(project_root),
        "DANGO_WORKSPACE": home,
        "DANGO_CLOUD_MODE": "true",
    }
    if tz:
        env["TZ"] = tz
    if run_id is not None and role == "script":
        env["DANGO_SCRIPT_RUN_ID"] = run_id
    if role == "nb":
        return env
    clash = sorted(set(declared) & set(env))
    if clash:
        raise ValueError(f"declared names collide with the base environment: {clash}")
    return {**env, **declared}


def build_command(
    role: str, args: Sequence[str], *, env_keys: Iterable[str], cwd: Path | str
) -> list[str]:
    """``sudo`` argv that starts ``isolated_run`` as the runner user (never via a shell).

    Values never appear here: they travel in the ``Popen`` environment and are
    handed through by ``--preserve-env=<names>``.
    """
    keys = sorted(env_keys)
    for key in keys:
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
    return [
        SUDO,
        "-n",
        "-u",
        runner_user(role),
        "-H",
        f"--preserve-env={','.join(keys)}",
        PYTHON,
        "-I",
        "-m",
        "dango.runtime.isolated_run",
        "--role",
        role,
        "--cwd",
        str(cwd),
        "--",
        *args,
    ]


def build_launch(
    role: str,
    args: Sequence[str],
    *,
    project_root: Path,
    names: Sequence[str] = (),
    legacy_argv: Sequence[str],
    run_id: str | None = None,
    tz: str | None = None,
    env_text: str | None = None,
) -> RunnerLaunch:
    """Launch description for *role*: isolated on a marked server, else legacy.

    *args* is what ``isolated_run`` receives after ``--`` (a script path relative to
    the project and its arguments, or the Marimo arguments).  *legacy_argv* is the
    command the caller ran before isolation existed; local mode returns it with a
    copy of ``os.environ``, exactly as before.
    """
    runner_user(role)
    if not uses_isolation():
        require_isolation_ready()
        return RunnerLaunch(list(legacy_argv), dict(os.environ), isolated=False)
    declared: dict[str, str] = {}
    missing: tuple[str, ...] = ()
    if role == "script":
        declared, missing = resolve_declared(project_root, names, env_text=env_text)
    env = build_env(role, project_root, declared, run_id=run_id, tz=tz)
    argv = build_command(role, args, env_keys=env, cwd=project_root)
    return RunnerLaunch(argv, env, isolated=True, missing=missing)


def _sudo_kill(role: str, signame: str, target: str) -> bool:
    cmd = [SUDO, "-n", "-u", runner_user(role), KILL, f"-{signame}", "--", target]
    return subprocess.run(cmd, capture_output=True, check=False, timeout=30).returncode == 0


def _group_alive(pgid: int) -> bool:
    """True while any process is left in group *pgid* (EPERM means one exists)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until(done: Callable[[], bool], grace: float) -> bool:
    for _ in range(max(1, int(grace / _POLL))):
        if done():
            return True
        time.sleep(_POLL)
    return done()


def stop_group(role: str, pgid: int, *, grace: float = 5.0) -> bool:
    """TERM then KILL process group *pgid* as the runner user.

    Returns:
        True if processes are still left in the group afterwards.
    """
    if pgid <= 1:
        raise ValueError(f"refusing to signal process group {pgid}")
    _sudo_kill(role, "TERM", f"-{pgid}")
    if not _wait_until(lambda: not _group_alive(pgid), grace):
        _sudo_kill(role, "KILL", f"-{pgid}")
        _wait_until(lambda: not _group_alive(pgid), 1.0)
    return _group_alive(pgid)


def runner_pids(role: str) -> list[int]:
    """Pids of all processes owned by the runner user (empty if the user is absent)."""
    import psutil

    try:
        uid = runner_uid(role)
    except KeyError:
        return []
    own = os.getpid()
    found: list[int] = []
    for proc in psutil.process_iter(["pid", "uids"]):
        uids = proc.info.get("uids")
        if uids is not None and uids.real == uid and proc.info["pid"] != own:
            found.append(int(proc.info["pid"]))
    return found


def stop_all(role: str, *, pgid: int | None = None, grace: float = 5.0) -> list[int]:
    """Stop the runner's processes and return the pids that remain.

    With *pgid*, only that process group is signalled (a script run).  Without it,
    every process of the runner user is, which is safe because each role has its
    own user.
    """
    if pgid is not None:
        return [pgid] if stop_group(role, pgid, grace=grace) else []
    _sudo_kill(role, "TERM", "-1")
    if not _wait_until(lambda: not runner_pids(role), grace):
        _sudo_kill(role, "KILL", "-1")
        _wait_until(lambda: not runner_pids(role), 1.0)
    return runner_pids(role)


def assert_runner_uid(uid: int) -> int:
    """Return *uid* if a runner may have it, else raise ``ValueError``."""
    if uid in RESERVED_UIDS or not _UID_MIN <= uid < _UID_MAX:
        raise ValueError(
            f"runner uid {uid} is not allowed (must be {_UID_MIN}-{_UID_MAX - 1} "
            f"and not one of {sorted(RESERVED_UIDS)})"
        )
    return uid


def assert_distinct_uids(*uids: int) -> None:
    """Raise ``ValueError`` if two runners share a uid."""
    if len(set(uids)) != len(uids):
        raise ValueError(f"runner users must have distinct uids, got {list(uids)}")


def runner_uid(role: str) -> int:
    """Checked uid of *role*'s user.

    Raises:
        KeyError: If the user does not exist.
        ValueError: If the uid is not allowed.
    """
    import pwd

    return assert_runner_uid(pwd.getpwnam(runner_user(role)).pw_uid)
