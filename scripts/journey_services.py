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

from journey_support import Journey, Step, failure, free_ports, port_is_open, safe

WEB_UP_TIMEOUT_SECONDS = 60
PORT_FREE_TIMEOUT_SECONDS = 10
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
        deadline = time.monotonic() + WEB_UP_TIMEOUT_SECONDS
        while True:
            if proc.poll() is not None:
                reason = f"{label}: process exited with code {proc.returncode} before it answered"
                break
            if _http_up(port):
                break
            if time.monotonic() > deadline:
                reason = f"{label}: no HTTP answer on port {port} within {WEB_UP_TIMEOUT_SECONDS}s"
                break
            time.sleep(0.5)
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
    deadline = time.monotonic() + PORT_FREE_TIMEOUT_SECONDS
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


def _compose_containers(project: str) -> list[str]:
    proc = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label=com.docker.compose.project={project}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return proc.stdout.split()


def _compose_volumes(project: str) -> list[str]:
    proc = subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    return proc.stdout.split()


def docker_step(j: Journey, project: str, platform_port: int) -> Step:
    """`dango start --yes`, poll the platform port, `dango status`, `dango stop`, check cleanup.

    `project` is the Compose project name of THIS run's project; cleanup is driven by it.
    """
    started = False
    result: Step | None = None
    try:
        started = True
        start = j.dango_run("start", "--yes")
        if start.returncode != 0:
            result = failure(
                "docker", start.error or f"start: exit code {start.returncode}", start.output
            )
        else:
            deadline = time.monotonic() + WEB_UP_TIMEOUT_SECONDS
            while not _http_up(platform_port) and time.monotonic() < deadline:
                time.sleep(1)
            if not _http_up(platform_port):
                result = failure(
                    "docker",
                    f"platform port {platform_port} did not answer after start",
                    start.output,
                )
        if result is None:
            status = j.dango_run("status")
            if status.returncode != 0:
                result = failure(
                    "docker",
                    status.error or f"status: exit code {status.returncode}",
                    status.output,
                )
    finally:
        if started:
            stop = j.dango_run("stop")
            if result is None and stop.returncode != 0:
                result = failure(
                    "docker", stop.error or f"stop: exit code {stop.returncode}", stop.output
                )
    if result is not None:
        _remove_leftovers(j, project)
        return result
    left = _compose_containers(project)
    if left:
        _remove_leftovers(j, project)
        return failure(
            "docker", f"{len(left)} container(s) of project {safe(project)} remain after stop"
        )
    if port_is_open(platform_port):
        return failure("docker", f"platform port {platform_port} still open after stop")
    _remove_leftovers(j, project)
    return Step("docker", "PASS", f"start, status, stop; no {safe(project)} containers remain")


def _remove_leftovers(j: Journey, project: str) -> None:
    """Remove containers and volumes labelled with THIS run's Compose project (best effort)."""
    try:
        for cid in _compose_containers(project):
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=60)
        for vol in _compose_volumes(project):
            subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        j.note(f"[docker cleanup incomplete: {exc}]")
