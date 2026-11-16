"""tests/unit/test_compose_exec_review.py

compose_exec review follow-ups: bounded wait after a forwarded signal, tolerant but safe
reading of the project's Dockerfile.metabase, and interrupted handling at the call sites.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest

from dango.config.models import DangoConfig, ProjectContext
from dango.platform import compose_exec as ce
from dango.platform.docker import DockerManager

pytestmark = pytest.mark.unit

TERM_SLOW = """#!/bin/sh
d=$(dirname "$0")
trap 'sleep 1; touch "$d/finished"; exit 130' INT TERM
echo started > "$d/started"
sleep 20 &
wait $!
"""
TERM_DEAF = """#!/bin/sh
d=$(dirname "$0")
trap '' INT TERM
echo started > "$d/started"
sleep 20 &
wait $!
"""
PS_SIGNAL = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/calls"
cat >/dev/null
case "$3" in
  image-exists|stop) kill -TERM "$(cat "$d/pytest.pid")"; sleep 0.3; exit 137;;
esac
exit 0
"""


def make_config(name: str = "p") -> DangoConfig:
    config = DangoConfig(project=ProjectContext(name=name, purpose="t", created_by="t"))
    config.platform.metabase_port = 3123
    config.platform.dbt_docs_port = 8123
    return config


@pytest.fixture
def sudo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "shim"
    d.mkdir()
    marker = tmp_path / "active"
    marker.write_text("")
    marker.chmod(0o644)
    monkeypatch.setattr(ce, "MARKER", str(marker))
    monkeypatch.setattr(ce, "_ROOT_UID", os.getuid())
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


def install_sudo(d: Path, text: str) -> None:
    (d / "sudo").write_text(text)
    (d / "sudo").chmod(0o755)


def raise_sigint_soon(pid: int, d: Path) -> None:
    def go() -> None:
        for _ in range(200):
            if (d / "started").exists():
                break
            time.sleep(0.02)
        os.kill(pid, signal.SIGINT)

    threading.Thread(target=go, daemon=True).start()


class TestBoundedWaitAfterSignal:
    def test_sudo_gets_time_to_stop_its_child_before_kill(
        self, sudo_dir: Path, tmp_path: Path
    ) -> None:
        install_sudo(sudo_dir, TERM_SLOW)
        raise_sigint_soon(os.getpid(), sudo_dir)
        with pytest.raises(KeyboardInterrupt):
            ce.run_compose(tmp_path, "up", timeout=60)
        assert (sudo_dir / "finished").exists()

    def test_wait_is_bounded(
        self, sudo_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ce, "SIGNAL_GRACE", 0.5)
        install_sudo(sudo_dir, TERM_DEAF)
        raise_sigint_soon(os.getpid(), sudo_dir)
        start = time.monotonic()
        with pytest.raises(KeyboardInterrupt):
            ce.run_compose(tmp_path, "up", timeout=60)
        assert time.monotonic() - start < 5


class TestDockerfileVersion:
    @pytest.mark.parametrize(
        "text",
        [
            "FROM x\r\nENV MB_VERSION=v0.77.2\r\nRUN y\r\n",
            "ENV MB_VERSION=v0.77.2 \t\nRUN y\n",
            'ENV MB_VERSION="v0.77.2"\n',
            "ENV MB_VERSION=v0.77.2",
        ],
    )
    def test_accepted_forms(self, tmp_path: Path, text: str) -> None:
        (tmp_path / "Dockerfile.metabase").write_bytes(text.encode())
        assert ce.render_args(tmp_path, make_config())[2] == "v0.77.2"

    @pytest.mark.parametrize(
        "text", ["ENV MB_VERSION=$(evil)\n", "FROM x\n", "", "ENV MB_VERSION=v1;x\n"]
    )
    def test_existing_but_unparseable_file_is_an_error_naming_it(
        self, tmp_path: Path, text: str
    ) -> None:
        (tmp_path / "Dockerfile.metabase").write_text(text)
        with pytest.raises(ce.ComposeRenderError, match="Dockerfile.metabase"):
            ce.render_args(tmp_path, make_config())

    def test_absent_file_uses_the_packaged_default(self, tmp_path: Path) -> None:
        assert ce.render_args(tmp_path, make_config())[2].startswith("v")


class TestDockerfileReadIsSafe:
    def test_fifo_does_not_block(self, tmp_path: Path) -> None:
        fifo = tmp_path / "Dockerfile.metabase"
        os.mkfifo(fifo)
        outcome: list[BaseException | None] = []

        def attempt() -> None:
            try:
                ce.render_args(tmp_path, make_config())
                outcome.append(None)
            except ce.ComposeRenderError as exc:
                outcome.append(exc)

        worker = threading.Thread(target=attempt, daemon=True)
        worker.start()
        worker.join(5)
        blocked = worker.is_alive()
        if blocked:  # release the reader so the test run itself cannot hang
            fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(5)
        assert not blocked, "render_args blocked on a FIFO"
        assert isinstance(outcome[0], ce.ComposeRenderError)
        assert "Dockerfile.metabase" in str(outcome[0])

    def test_symlink_is_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.write_text("ENV MB_VERSION=v0.77.2\n")
        (tmp_path / "Dockerfile.metabase").symlink_to(real)
        with pytest.raises(ce.ComposeRenderError, match="Dockerfile.metabase"):
            ce.render_args(tmp_path, make_config())

    def test_directory_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile.metabase").mkdir()
        with pytest.raises(ce.ComposeRenderError, match="Dockerfile.metabase"):
            ce.render_args(tmp_path, make_config())

    def test_only_the_first_64_kib_are_read(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile.metabase").write_text("# x\n" * 20000 + "ENV MB_VERSION=v0.77.2\n")
        with pytest.raises(ce.ComposeRenderError, match="Dockerfile.metabase"):
            ce.render_args(tmp_path, make_config())

    def test_version_inside_the_first_64_kib_is_found(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile.metabase").write_text("ENV MB_VERSION=v0.77.2\n" + "# x\n" * 20000)
        assert ce.render_args(tmp_path, make_config())[2] == "v0.77.2"


class TestInterruptedAtCallSites:
    def test_start_services_reports_and_does_not_continue(
        self, sudo_dir: Path, tmp_path: Path
    ) -> None:
        (sudo_dir / "pytest.pid").write_text(str(os.getpid()))
        install_sudo(sudo_dir, PS_SIGNAL)
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            assert DockerManager(tmp_path / "p").start_services() is False
        finally:
            signal.signal(signal.SIGTERM, previous)
        verbs = [line.split()[2] for line in (sudo_dir / "calls").read_text().splitlines()]
        assert "up" not in verbs

    def test_stop_all_reports_failure(self, sudo_dir: Path, tmp_path: Path) -> None:
        (sudo_dir / "pytest.pid").write_text(str(os.getpid()))
        install_sudo(sudo_dir, PS_SIGNAL)
        previous = signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            assert DockerManager(tmp_path / "p").stop_all_dango_containers() is False
        finally:
            signal.signal(signal.SIGTERM, previous)
