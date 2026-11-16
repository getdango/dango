"""scripts/journey_services.py

Service steps of scripts/first_user_journey.py: `dango web` start/stop/restart without Docker,
and the opt-in Docker platform start/status/stop. Every process or container the steps start
is stopped in a finally block. Standard library only; safe on Windows, macOS and Linux.
"""

import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

from journey_support import BUDGET_EXHAUSTED, Journey, Step, failure, free_ports, port_is_open, safe

WEB_UP_TIMEOUT_SECONDS = 60
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_up(port: int) -> bool:
    """Any HTTP response (200, 302, 401 ...) means the server is up."""
    try:
        _OPENER.open(f"http://127.0.0.1:{port}/", timeout=3).close()
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False
    return True


def _tail(path: Path, lines: int = 15) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def _stop(proc: "subprocess.Popen[bytes]", grace: float) -> None:
    """terminate(), wait `grace` seconds, then kill(); the process is gone on return."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=grace + 15)


def _run_web_once(j: Journey, port: int, label: str, grace: float) -> str | None:
    """Start `dango web`, wait until it answers, stop it. Returns a failure reason or None."""
    if j.remaining() <= 0:
        return f"{label}: {BUDGET_EXHAUSTED}"
    out_path = j.workdir / f"journey-web-{label}.log"
    out: IO[bytes] | None = None
    proc: subprocess.Popen[bytes] | None = None
    reason: str | None = None
    try:
        out = open(out_path, "wb")
        argv = [j.dango, "web", "--host", "127.0.0.1", "--port", str(port)]
        j.note(f"$ {' '.join(argv)}  [{label}]")
        proc = subprocess.Popen(
            argv, cwd=j.workdir, env=j.env(), stdout=out, stderr=subprocess.STDOUT
        )
        wait = min(WEB_UP_TIMEOUT_SECONDS, j.remaining())
        deadline = time.monotonic() + wait
        while True:
            if proc.poll() is not None:
                reason = f"{label}: process exited with code {proc.returncode} before it answered"
                break
            if _http_up(port):
                break
            if time.monotonic() > deadline:
                reason = f"{label}: no HTTP answer on port {port} within {wait:g}s"
                break
            time.sleep(0.2)
    except OSError as exc:
        reason = f"{label}: could not start {j.dango}: {exc}"
    finally:
        if proc is not None:
            _stop(proc, grace)
        if out is not None:
            out.close()
        j.note(f"[web {label} output]\n{_tail(out_path, 200)}")
    if reason is not None:
        return reason + "\n" + _tail(out_path)
    deadline = time.monotonic() + j.port_free_timeout
    while port_is_open(port):
        if time.monotonic() > deadline:
            return f"{label}: port {port} still in use after the process was stopped"
        time.sleep(0.25)
    return None


def web_step(j: Journey, grace: float) -> Step:
    """Start `dango web`, check it answers, stop it, then do it all again (restart check)."""
    port = free_ports(1)[0]
    for label in ("first start", "restart"):
        reason = _run_web_once(j, port, label, grace)
        if reason is not None:
            head, _, tail = reason.partition("\n")
            return failure("web", head, tail)
    return Step("web", "PASS", f"answered on port {port}, stopped, and restarted cleanly")


class DockerQueryError(Exception):
    """A docker CLI query failed, so its (empty) answer cannot be trusted."""


def _docker_query(argv: list[str]) -> list[str]:
    """Run a docker CLI query; return its output words, or raise DockerQueryError."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerQueryError(f"{' '.join(argv[:3])} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()[-1:] or [""]
        raise DockerQueryError(f"{' '.join(argv[:3])} exited {proc.returncode}: {detail[0]}")
    return proc.stdout.split()


def _compose_containers(project: str) -> list[str]:
    label = f"label=com.docker.compose.project={project}"
    return _docker_query(["docker", "ps", "-a", "-q", "--filter", label])


def _compose_volumes(project: str) -> list[str]:
    label = f"label=com.docker.compose.project={project}"
    return _docker_query(["docker", "volume", "ls", "-q", "--filter", label])


_PROJECT_INFO = (
    "import sys\n"
    "from pathlib import Path\n"
    "import yaml\n"
    "from dango.platform.docker import get_compose_project_name\n"
    "root = Path(sys.argv[1])\n"
    "d = yaml.safe_load((root / '.dango' / 'project.yml').read_text(encoding='utf-8')) or {}\n"
    "print(get_compose_project_name(root))\n"
    "print((d.get('platform') or {}).get('port', 8800))\n"
)


def _project_info(j: Journey) -> tuple[str, int] | str:
    """(Compose project name, platform port) of this run's project, or an error message.

    The name comes from dango's own get_compose_project_name (including its fallback), run
    with the same python that runs dango.
    """
    proc = j.run([j.python, "-c", _PROJECT_INFO, str(j.workdir)])
    words = proc.output.split()
    if proc.returncode != 0 or len(words) < 2 or not words[-1].isdigit():
        return proc.error or "could not read the Compose project name and platform port"
    return words[-2], int(words[-1])


def docker_step(j: Journey) -> Step:
    """`dango start --yes`, poll the platform port, `dango status`, `dango stop`, check cleanup.

    Cleanup is driven by this run's Compose project name; `dango stop` always runs once
    `start` was attempted. The step cannot pass unless containers of the project existed while
    the platform was up and none remain after the stop.
    """
    info = _project_info(j)
    if isinstance(info, str):
        return failure("docker", info)
    project, platform_port = info
    result: Step | None = None
    try:
        start = j.dango_run("start", "--yes")
        if start.returncode != 0:
            result = failure(
                "docker", start.error or f"start: exit code {start.returncode}", start.output
            )
        else:
            deadline = time.monotonic() + min(WEB_UP_TIMEOUT_SECONDS, j.remaining())
            while not _http_up(platform_port) and time.monotonic() < deadline:
                time.sleep(0.5)
            if not _http_up(platform_port):
                result = failure(
                    "docker",
                    f"platform port {platform_port} did not answer after start",
                    start.output,
                )
            elif not _compose_containers(project):
                result = failure(
                    "docker",
                    f"no containers of project {safe(project)} existed while the platform was up",
                )
        if result is None:
            status = j.dango_run("status")
            if status.returncode != 0:
                result = failure(
                    "docker",
                    status.error or f"status: exit code {status.returncode}",
                    status.output,
                )
    except DockerQueryError as exc:
        result = failure("docker", safe(str(exc)))
    finally:
        stop = j.dango_run("stop", ignore_budget=True)
        if result is None and stop.returncode != 0:
            result = failure(
                "docker", stop.error or f"stop: exit code {stop.returncode}", stop.output
            )
    try:
        if result is None:
            left = _compose_containers(project)
            if left:
                result = failure(
                    "docker",
                    f"{len(left)} container(s) of project {safe(project)} remain after stop",
                )
            elif port_is_open(platform_port):
                result = failure("docker", f"platform port {platform_port} still open after stop")
    except DockerQueryError as exc:
        result = failure("docker", safe(str(exc)))
    cleanup_error = _remove_leftovers(j, project)
    if result is None and cleanup_error:
        result = failure("docker", cleanup_error)
    return result or Step(
        "docker", "PASS", f"start, status, stop; no {safe(project)} containers remain"
    )


def _remove_leftovers(j: Journey, project: str) -> str | None:
    """Remove containers and volumes labelled with THIS run's Compose project.

    Returns an error message when a docker query or removal failed, else None.
    """
    try:
        for cid in _compose_containers(project):
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=30)
        for vol in _compose_volumes(project):
            subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True, timeout=30)
    except (DockerQueryError, OSError, subprocess.TimeoutExpired) as exc:
        j.note(f"[docker cleanup incomplete: {exc}]")
        return safe(f"docker cleanup incomplete: {exc}")
    return None
