"""tests/unit/test_docker_hints.py

OS-correct wording for a missing or stopped Docker, and the one-line note for skipped Metabase steps.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.unit


def _text(binary_present: bool, platform: str) -> str:
    from dango.utils.docker_hints import docker_guidance_lines

    return "\n".join(docker_guidance_lines(binary_present=binary_present, platform=platform))


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_desktop_platforms_name_docker_desktop(platform: str) -> None:
    missing = _text(False, platform)
    stopped = _text(True, platform)
    assert "Docker Desktop" in missing and "not installed" in missing
    assert "https://docs.docker.com/desktop/" in missing
    assert "Docker Desktop" in stopped and "start" in stopped.lower()
    assert "systemctl" not in missing + stopped
    assert "Docker Engine" not in missing + stopped


def test_linux_names_docker_engine_and_the_daemon_command() -> None:
    missing = _text(False, "linux")
    stopped = _text(True, "linux")
    assert "Docker Engine" in missing and "compose plugin" in missing
    assert "https://docs.docker.com/engine/install/" in missing
    assert "systemctl start docker" in stopped
    assert "Docker Desktop" not in missing + stopped


def test_missing_and_stopped_wording_differ() -> None:
    for platform in ("darwin", "win32", "linux"):
        assert _text(False, platform) != _text(True, platform)


def test_default_platform_is_sys_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    from dango.utils.docker_hints import docker_guidance_lines

    monkeypatch.setattr(sys, "platform", "linux")
    assert "systemctl" in "\n".join(docker_guidance_lines(binary_present=True))
    monkeypatch.setattr(sys, "platform", "win32")
    assert "Docker Desktop" in "\n".join(docker_guidance_lines(binary_present=True))


def test_guidance_is_ascii_for_a_cp1252_console() -> None:
    for platform in ("darwin", "win32", "linux"):
        for present in (True, False):
            _text(present, platform).encode("cp1252")
            _text(present, platform).encode("ascii")


def test_binary_presence_follows_the_path(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from dango.utils.docker_hints import docker_binary_present

    monkeypatch.setenv("PATH", str(tmp_path))
    assert docker_binary_present() is False


def test_not_available_error_keeps_the_legacy_daemon_message() -> None:
    from dango.utils.docker_hints import DockerNotAvailableError

    stopped = DockerNotAvailableError(binary_present=True)
    missing = DockerNotAvailableError(binary_present=False)
    assert isinstance(stopped, RuntimeError)
    assert "Docker daemon is not running" in str(stopped)
    assert "Docker daemon is not running" not in str(missing)
    assert missing.binary_present is False and stopped.binary_present is True


def test_metabase_note_prints_once_and_is_plain_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    from io import StringIO

    from rich.console import Console

    from dango.utils import docker_hints

    monkeypatch.setitem(docker_hints._printed, "metabase", False)
    buf = StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    docker_hints.note_metabase_skipped(console)
    docker_hints.note_metabase_skipped(console)
    out = buf.getvalue()
    assert out.count("Metabase not refreshed: Docker is not available") == 1
    out.encode("cp1252")


def test_linux_stopped_daemon_mentions_the_docker_group() -> None:
    text = _text(True, "linux")
    assert "usermod -aG docker" in text and "log out" in text


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_missing_desktop_install_says_to_open_a_new_terminal(platform: str) -> None:
    assert "new terminal" in _text(False, platform)


def test_missing_linux_install_says_to_open_a_new_terminal() -> None:
    assert "new terminal" in _text(False, "linux")


def test_reachability_probe_tells_missing_from_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import subprocess
    from unittest.mock import patch

    from dango.utils.docker_hints import docker_state

    monkeypatch.setenv("PATH", str(tmp_path))
    assert docker_state() == "missing"
    down = subprocess.CompletedProcess(["docker", "ps"], 1, stdout="", stderr="")
    up = subprocess.CompletedProcess(["docker", "ps"], 0, stdout="", stderr="")
    with patch("shutil.which", return_value="/usr/bin/docker"):
        with patch("subprocess.run", return_value=down):
            assert docker_state() == "stopped"
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("docker", 5)):
            assert docker_state() == "stopped"
        with patch("subprocess.run", return_value=up):
            assert docker_state() == "running"
