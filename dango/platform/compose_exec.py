"""dango/platform/compose_exec.py

Single dango-side entry point for running Docker Compose.

On a server where the root-owned compose helper is installed (marker file present),
compose runs through ``sudo -n /usr/local/sbin/dango-compose <verb>``; otherwise the
legacy direct ``docker compose`` call is made exactly as before.  Nothing in this
repository writes the marker yet, so every helper path is inert.

Run as ``python -I -m dango.platform.compose_exec render [--helper] PROJECT_ROOT`` to
render the server compose file through the helper.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import stat
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Kept equal to ``cloud.compose_helper`` (a test asserts it); not imported so that the
# local path does not load the whole cloud package.
HELPER = "/usr/local/sbin/dango-compose"
MARKER = "/srv/dango/compose/active"
PROJECT_ID_FILE = "/srv/dango/compose/project-id"
_ROOT_UID = 0
#: Seconds sudo gets to stop the helper after a forwarded signal before it is killed.
SIGNAL_GRACE = 15
_DOCKERFILE_MAX = 64 * 1024

#: Helper exit codes: busy lock; budget expired (124); killed (137, 143).
EXIT_BUSY = 75
_KILLED = (124, 137, 143)

#: Local compose tokens per verb; ``None`` marks a verb that has no direct form.
_LOCAL_TOKENS: dict[str, tuple[str, ...] | None] = {
    "up": ("up", "-d"),
    "down": ("down",),
    "ps": ("ps", "--format", "json"),
    "stop": ("stop",),
    "start": ("start",),
    "restart": ("restart",),
    "image-exists": None,
}
_VERSION_LINE = re.compile(
    r"^ENV MB_VERSION=(\"?)(v[0-9][0-9.]{0,18})\1[ \t]*\r?(?:\n|\Z)", re.MULTILINE
)
_VERSION_ANY = re.compile(r"^[ \t]*ENV\b[^\n]*\bMB_VERSION\b", re.MULTILINE | re.IGNORECASE)
_BUDGET_LINE = re.compile(r"^dango-compose: .* exceeded its time budget.*\n?", re.MULTILINE)


class ComposeBusy(RuntimeError):
    """Another Docker operation holds the helper's lock (helper exit 75)."""


class ComposeInterrupted(RuntimeError):
    """The wrapper itself was signalled while the helper ran."""


class ComposeRenderError(RuntimeError):
    """The helper could not render the server compose file."""


def uses_helper() -> bool:
    """True when the marker is a root-owned regular file nobody else can write."""
    try:
        st = os.lstat(MARKER)
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == _ROOT_UID
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def effective_timeout(verb: str, timeout: float, *, helper: bool) -> float:
    """Caller timeout; through the helper never below lock wait + verb budget + margin."""
    if not helper:
        return timeout
    from dango.platform.cloud.compose_helper import min_caller_timeout

    return max(timeout, min_caller_timeout(verb))


def _local_run(
    project_root: Path, verb: str, args: Sequence[str], timeout: float, opts: dict[str, Any]
) -> subprocess.CompletedProcess[Any]:
    tokens = _LOCAL_TOKENS.get(verb)
    if tokens is None:
        raise ValueError(f"no direct compose form for verb: {verb}")
    compose_cmd = opts.get("compose_cmd") or ["docker", "compose"]
    env = opts.get("env")
    if env is None:
        from dango.platform.docker import get_compose_project_name

        env = {**os.environ, "COMPOSE_PROJECT_NAME": get_compose_project_name(project_root)}
    kwargs: dict[str, Any] = {}
    for key in ("cwd", "stdin"):
        if opts.get(key) is not None:
            kwargs[key] = opts[key]
    kwargs["capture_output"] = True
    if opts.get("text") is not None:
        kwargs["text"] = opts["text"]
    argv = [*compose_cmd, "-f", str(project_root / "docker-compose.yml"), *tokens, *args]
    return subprocess.run(argv, timeout=timeout, env=env, **kwargs)


class _Watch:
    """Notes SIGINT/SIGTERM/SIGHUP while the helper runs and forwards them to it."""

    def __init__(self) -> None:
        self.hit = False
        self.proc: subprocess.Popen[Any] | None = None
        self._saved: dict[int, Any] = {}
        self._redeliver: list[int] = []

    def __enter__(self) -> _Watch:
        if threading.current_thread() is not threading.main_thread():
            return self
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous = signal.getsignal(sig)
            if previous == signal.SIG_IGN:
                continue
            self._saved[sig] = previous
            signal.signal(sig, self._handle)
        return self

    def _handle(self, signum: int, frame: Any) -> None:
        self.hit = True
        if self.proc is not None:
            try:
                self.proc.send_signal(signum)
            except OSError:
                pass
        previous = self._saved.get(signum)
        if callable(previous):
            previous(signum, frame)
        else:
            self._redeliver.append(signum)

    def __exit__(self, *exc: object) -> None:
        for sig, previous in self._saved.items():
            signal.signal(sig, previous)
        for sig in self._redeliver:
            os.kill(os.getpid(), sig)


def _helper_run(
    verb: str, args: Sequence[str], timeout: float, text: bool
) -> subprocess.CompletedProcess[Any]:
    argv = ["sudo", "-n", HELPER, verb, *args]
    with _Watch() as watch:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=text,
        )
        watch.proc = proc
        if watch.hit:
            proc.terminate()
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        except BaseException:
            if not watch.hit and proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=SIGNAL_GRACE)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
    rc = proc.returncode
    killed = rc in _KILLED or rc < 0
    err_text = err if text else err.decode(errors="replace")
    if watch.hit and killed:
        cleaned = _BUDGET_LINE.sub("", err_text).strip()
        raise ComposeInterrupted(f"dango-compose {verb}: interrupted\n{cleaned}".strip())
    if rc == EXIT_BUSY:
        raise ComposeBusy(err_text.strip() or "dango-compose: busy")
    if killed:
        raise subprocess.TimeoutExpired(argv, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(argv, rc, out, err)


def run_compose(
    project_root: Path,
    verb: str,
    *args: str,
    timeout: float,
    compose_cmd: Sequence[str] | None = None,
    cwd: Path | None = None,
    stdin: int | None = None,
    env: dict[str, str] | None = None,
    text: bool | None = None,
) -> subprocess.CompletedProcess[Any]:
    """Run one compose verb; ``subprocess``-style result, output captured.

    Through the helper, *compose_cmd*, *cwd*, *stdin* and *env* are ignored, stdin is
    closed and *timeout* is raised to cover the helper's own lock wait and budget.
    Raises ``ComposeBusy`` (lock held), ``ComposeInterrupted`` (this process was
    signalled) or ``subprocess.TimeoutExpired`` (helper budget expired or killed).
    """
    if uses_helper():
        wait = effective_timeout(verb, timeout, helper=True)
        return _helper_run(verb, args, wait, bool(text))
    opts = {"compose_cmd": compose_cmd, "cwd": cwd, "stdin": stdin, "env": env, "text": text}
    return _local_run(project_root, verb, args, timeout, opts)


def _project_mb_version(path: Path) -> str | None:
    """MB_VERSION from the project's Dockerfile.metabase; ``None`` when the file is absent.

    The file is dango-writable, so it is opened without following links or blocking, must
    be a regular file, and at most 64 KiB are read.  Present but unusable is an error.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ComposeRenderError(f"cannot read {path.name}: {exc.strerror}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ComposeRenderError(f"{path.name} is not a regular file")
        data = os.read(fd, _DOCKERFILE_MAX)
    finally:
        os.close(fd)
    version = _mb_version(data.decode("utf-8", errors="replace"))
    if version is None:
        raise ComposeRenderError(f"{path.name} has no usable ENV MB_VERSION line")
    return version


def _mb_version(text: str) -> str | None:
    """The one MB_VERSION a Dockerfile text sets; ``None`` when it sets none.

    Docker uses the last ``ENV MB_VERSION``, so any line this reader cannot parse, or two
    lines that disagree, make the answer ambiguous and are refused.
    """
    found = {m.group(2) for m in _VERSION_LINE.finditer(text)}
    lines = len(_VERSION_ANY.findall(text))
    if len(found) > 1 or (found and lines != len(list(_VERSION_LINE.finditer(text)))):
        raise ComposeRenderError("Dockerfile.metabase has conflicting ENV MB_VERSION lines")
    return next(iter(found), None)


def render_args(project_root: Path, config: Any) -> list[str]:
    """Arguments for the helper's ``render``; creates the bind sources dango owns."""
    (project_root / "data").mkdir(exist_ok=True)
    (project_root / "dbt" / "target").mkdir(parents=True, exist_ok=True)
    version = _project_mb_version(project_root / "Dockerfile.metabase")
    if version is None:
        packaged = Path(__file__).parents[1] / "templates" / "Dockerfile.metabase"
        version = _mb_version(packaged.read_text(encoding="utf-8"))
        if version is None:
            raise ComposeRenderError("packaged Dockerfile.metabase has no MB_VERSION")
    name = config.project.name.lower().replace(" ", "-")
    return [str(config.platform.metabase_port), str(config.platform.dbt_docs_port), version, name]


def _warn_if_id_differs(config: Any) -> None:
    try:
        pinned = Path(PROJECT_ID_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return
    if pinned and pinned != config.project.id:
        logger.warning(
            "compose_project_id_mismatch: project.yml id differs from the id pinned on this "
            "server; keeping the pinned id. A root user can change it with "
            "'dango-compose pin-project ID --replace' (this recreates the Metabase volume)."
        )


def render(project_root: Path, config: Any, *, force_helper: bool = False) -> None:
    """Write the compose file: through the helper on a server, else locally as before."""
    if not (force_helper or uses_helper()):
        from dango.platform.docker import render_docker_compose

        render_docker_compose(project_root, config)
        return
    _warn_if_id_differs(config)
    args = render_args(project_root, config)
    argv = ["sudo", "-n", HELPER, "render", *args]
    done = subprocess.run(
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=effective_timeout("render", 0, helper=True),
        check=False,
    )
    if done.returncode == EXIT_BUSY:
        raise ComposeBusy(done.stderr.strip() or "dango-compose: busy")
    if done.returncode != 0:
        raise ComposeRenderError(done.stderr.strip() or f"render failed ({done.returncode})")


def main(argv: list[str]) -> int:
    """CLI: ``render [--helper] PROJECT_ROOT``."""
    rest = argv[1:]
    force = bool(rest) and rest[0] == "--helper"
    if force:
        rest = rest[1:]
    if argv[:1] != ["render"] or len(rest) != 1:
        print("usage: compose_exec render [--helper] PROJECT_ROOT", file=sys.stderr)
        return 2
    from dango.config.helpers import load_config

    root = Path(rest[0])
    try:
        render(root, load_config(root), force_helper=force)
    except (ComposeRenderError, ComposeBusy, subprocess.TimeoutExpired) as exc:
        print(f"compose_exec: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
