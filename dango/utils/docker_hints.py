"""dango/utils/docker_hints.py

Wording for a missing or stopped Docker (OS-correct) and the one-line note for skipped Metabase steps.

Level 0 module: standard library and ``rich.markup`` only. Runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import shutil
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
            ]
        return [
            "Docker Desktop is not running.",
            "Start Docker Desktop and wait until it reports that it is running.",
        ]
    if not binary_present:
        return [
            "Docker Engine is not installed (no 'docker' command was found).",
            "Install Docker Engine and the compose plugin: https://docs.docker.com/engine/install/",
        ]
    return [
        "The Docker daemon is not running (or this user cannot reach it).",
        "Start it with: sudo systemctl start docker",
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


def note_metabase_skipped(console: Any) -> None:
    """Print the one-line note that Metabase was not refreshed (once per process)."""
    if _printed["metabase"]:
        return
    _printed["metabase"] = True
    console.print(f"[dim]{METABASE_SKIPPED_TEXT}[/dim]")
