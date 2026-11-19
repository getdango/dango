"""dango/utils/docker_hints.py

Wording for a missing or stopped Docker (OS-correct) and the one-line note for skipped Metabase steps.

Level 0 module: standard library and ``rich.markup`` only. Runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

# ``refresh_metabase_connection`` returns this as its error text when Docker cannot be used.
DOCKER_UNAVAILABLE_ERROR = "Docker is not available"
METABASE_SKIPPED_TEXT = "Metabase not refreshed: Docker is not available"

_DESKTOP_PLATFORMS = ("darwin", "win32")
_printed: dict[str, bool] = {"metabase": False}


def _current_platform() -> str:
    return sys.platform


def docker_binary_present() -> bool:
    """True when a ``docker`` executable is on ``PATH`` (says nothing about the daemon)."""
    return shutil.which("docker") is not None


def docker_state() -> str:
    """ "missing" (no binary), "stopped" (daemon unreachable or hung) or "running"."""
    if not docker_binary_present():
        return "missing"
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "stopped"
    except OSError:
        return "missing"
    return "running" if result.returncode == 0 else "stopped"


def docker_unreachable_note(state: str, consequence: str) -> str | None:
    """One dim line for a missing or stopped Docker (``state`` from ``docker_state``), else None."""
    if state == "missing":
        return f"[dim]Docker is not available, so {consequence}.[/dim]"
    if state == "stopped":
        return f"[dim]Docker is not running, so {consequence}.[/dim]"
    return None


def docker_guidance_lines(*, binary_present: bool, platform: str | None = None) -> list[str]:
    """Plain, ASCII guidance for a missing (``binary_present`` False) or stopped Docker.

    macOS and Windows get Docker Desktop wording; every other platform gets Docker Engine
    wording. ``platform`` defaults to ``sys.platform``.
    """
    current = platform if platform is not None else _current_platform()
    if current in _DESKTOP_PLATFORMS:
        if not binary_present:
            return [
                "Docker Desktop is not installed (no 'docker' command was found).",
                "Install Docker Desktop from https://docs.docker.com/desktop/ and start it.",
                "If you just installed it, open a new terminal so the 'docker' command is found.",
            ]
        return [
            "Docker Desktop is not running.",
            "Start Docker Desktop and wait until it reports that it is running.",
        ]
    if not binary_present:
        return [
            "Docker Engine is not installed (no 'docker' command was found).",
            "Install Docker Engine and the compose plugin: https://docs.docker.com/engine/install/",
            "If you just installed it, open a new terminal so the 'docker' command is found.",
        ]
    return [
        "The Docker daemon is not running (or this user cannot reach it).",
        "Start it with: sudo systemctl start docker",
        "If it is running, add your user to the docker group: sudo usermod -aG docker $USER",
        "(then log out and back in).",
    ]


class DockerNotAvailableError(RuntimeError):
    """Docker is missing (``binary_present`` False) or its daemon is not running."""

    def __init__(self, *, binary_present: bool) -> None:
        self.binary_present = binary_present
        if binary_present:
            message = "Docker daemon is not running"
        else:
            message = "Docker is not installed or not on PATH"
        super().__init__(message)


def metabase_skip_reported() -> bool:
    """True once ``note_metabase_skipped`` has printed in this process."""
    return _printed["metabase"]


def note_metabase_skipped(console: Any) -> None:
    """Print the one-line note that Metabase was not refreshed (once per process)."""
    if _printed["metabase"]:
        return
    _printed["metabase"] = True
    console.print(f"[dim]{METABASE_SKIPPED_TEXT}[/dim]")
