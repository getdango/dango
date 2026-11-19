"""tests/unit/test_build_failure_output.py

Metabase image build failures: last lines of output, redacted and escaped, plus a DNS hint when it applies.
"""

from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from dango.platform.docker import DockerManager

pytestmark = pytest.mark.unit

_DNS_OUTPUTS = [
    "failed to solve: Get https://registry-1.docker.io/v2/: dial tcp: lookup registry-1.docker.io: no such host",
    "E: Temporary failure in name resolution",
    "curl: (6) Could not resolve host: github.com",
    "dial tcp: lookup repo.maven.apache.org on 192.168.65.7:53: server misbehaving",
]
_PLAIN = "failed to solve: process apt-get install returned a non-zero code: 100"


def _start(tmp_path: Path, stderr: str, stdout: str = "") -> str:
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    manager = DockerManager(tmp_path)
    buf = StringIO()
    console = Console(file=buf, width=300, force_terminal=False, color_system=None)
    done = subprocess.CompletedProcess(["docker"], 1, stdout=stdout, stderr=stderr)
    with (
        patch.object(manager, "_resolve_or_migrate_project_id"),
        patch.object(manager, "_assert_no_identity_collision"),
        patch.object(manager, "is_docker_available", return_value=True),
        patch.object(manager, "is_compose_available", return_value=True),
        patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
        patch.object(manager, "_metabase_image_exists", return_value=False),
        patch("dango.platform.compose_exec.run_compose", return_value=done),
        patch("dango.platform.docker.console", console),
    ):
        assert manager.start_services() is False
    return buf.getvalue()


@pytest.mark.parametrize("line", _DNS_OUTPUTS)
def test_dns_failures_get_a_hint(tmp_path: Path, line: str) -> None:
    out = _start(tmp_path, f"#5 RUN apt-get update\n{line}\n")
    assert line in out
    assert "DNS" in out and "daemon" in out
    assert "docker build --network host" in out
    for destructive in ("prune", "rm -f", "rmi", "system reset", "down -v"):
        assert destructive not in out


def test_unrelated_failures_get_no_dns_hint(tmp_path: Path) -> None:
    out = _start(tmp_path, _PLAIN)
    assert _PLAIN in out
    assert "DNS" not in out and "--network host" not in out


def test_only_the_last_lines_are_shown(tmp_path: Path) -> None:
    lines = [f"step {i}" for i in range(200)]
    out = _start(tmp_path, "\n".join(lines))
    assert "step 199" in out and "step 170" in out
    assert "step 100" not in out and "step 0\n" not in out


def test_build_output_is_redacted(tmp_path: Path) -> None:
    out = _start(tmp_path, "pull from https://user:hunter2pass@registry.example.com failed\n")
    assert "hunter2pass" not in out
    out = _start(tmp_path, "ENV API_TOKEN=abcdef123456789 leaked\n")
    assert "abcdef123456789" not in out


def test_build_output_is_not_parsed_as_markup(tmp_path: Path) -> None:
    out = _start(tmp_path, "step [bold]x[/bold] and [/oops] and C:\\a\\[b]\n")
    assert "[bold]x[/bold]" in out and "[/oops]" in out


def test_stdout_is_used_when_stderr_is_empty(tmp_path: Path) -> None:
    out = _start(tmp_path, "", stdout="#7 ERROR: no such host\n")
    assert "no such host" in out and "DNS" in out


def test_port_conflict_keeps_its_own_message(tmp_path: Path) -> None:
    out = _start(tmp_path, "Bind for 0.0.0.0:3000 failed: port is already allocated")
    assert "dango stop" in out
    assert "DNS" not in out


def test_report_helper_limits_lines_and_flags_dns() -> None:
    from dango.utils.build_output import build_failure_report, looks_like_dns_failure

    assert looks_like_dns_failure("dial tcp: lookup x: no such host")
    assert looks_like_dns_failure("TEMPORARY FAILURE IN NAME RESOLUTION")
    assert not looks_like_dns_failure("permission denied")
    report = build_failure_report(None, "\n".join(str(i) for i in range(100)), max_lines=5)
    assert "99" in report and "94" not in report
