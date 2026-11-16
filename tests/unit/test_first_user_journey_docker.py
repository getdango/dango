"""tests/unit/test_first_user_journey_docker.py

The opt-in docker step of scripts/first_user_journey.py (start, status, stop, leftover check),
driven with a fake dango and a fake docker on PATH.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.factories.journey_fakes import reap_orphans, run_journey

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_orphan_fake_processes(tmp_path: Path) -> Iterator[None]:
    """Fail any test that leaves a fake platform process running (and kill it)."""
    yield
    left = reap_orphans(tmp_path)
    assert not left, f"fake processes survived the test: {left}"


def _calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "work" / "docker-calls.txt"
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def _docker_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if re.match(r"(PASS|FAIL|SKIP) docker", ln))


def test_docker_step_passes_and_leaves_nothing_behind(tmp_path: Path) -> None:
    result = run_journey(tmp_path, docker=True)
    assert result.returncode == 0, result.stdout
    assert any(ln.startswith("PASS docker") for ln in result.stdout.splitlines())
    assert _calls(tmp_path) == ["start", "status", "stop"]
    assert (tmp_path / "work" / "containers.txt").read_text(encoding="utf-8").strip() == ""


def test_containers_left_after_stop_fail_the_step_and_are_removed(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "leftover", docker=True)
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "remain" in failing
    removed = (tmp_path / "work" / "docker-rm.txt").read_text(encoding="utf-8")
    assert "abc123def456" in removed


def test_docker_ps_failure_is_a_fail_not_zero_containers(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "ps-fails", docker=True)
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
    assert "PASS docker" not in result.stdout


def test_docker_volume_ls_failure_is_a_fail(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "volume-ls-fails", docker=True)
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")


def test_start_failure_fails_and_stop_is_still_called(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "fail-start", docker=True)
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
    assert _calls(tmp_path) == ["start", "stop"]


def test_status_failure_fails_and_stop_is_still_called(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "fail-status", docker=True)
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
    assert _calls(tmp_path) == ["start", "status", "stop"]


def test_a_stack_with_no_containers_while_running_cannot_pass(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "start-no-containers", docker=True)
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "no containers" in failing


def test_project_name_comes_from_the_compose_helper_not_a_guess(tmp_path: Path) -> None:
    # The fake docker only knows containers under dango-abcdef12 (from project.id).
    result = run_journey(tmp_path, docker=True, env={"FAKE_PROJECT": "dango-someone-else"})
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
