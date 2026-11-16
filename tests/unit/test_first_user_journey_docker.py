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
    path = tmp_path / "fakestate" / "docker-calls.txt"
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def _docker_line(stdout: str) -> str:
    return next(ln for ln in stdout.splitlines() if re.match(r"(PASS|FAIL|SKIP) docker", ln))


def test_docker_step_passes_and_leaves_nothing_behind(tmp_path: Path) -> None:
    result = run_journey(tmp_path, docker=True, steps="init,docker")
    assert result.returncode == 0, result.stdout
    assert any(ln.startswith("PASS docker") for ln in result.stdout.splitlines())
    assert _calls(tmp_path) == ["start", "status", "stop"]
    assert (tmp_path / "fakestate" / "containers.txt").read_text(encoding="utf-8").strip() == ""


def test_containers_left_after_stop_fail_the_step_and_are_removed(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "leftover", docker=True, steps="init,docker")
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "remain" in failing
    removed = (tmp_path / "fakestate" / "docker-rm.txt").read_text(encoding="utf-8")
    assert "abc123def456" in removed


def test_docker_ps_failure_is_a_fail_not_zero_containers(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "ps-fails", docker=True, steps="init,docker")
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
    assert "PASS docker" not in result.stdout


def test_docker_volume_ls_failure_is_a_fail(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "volume-ls-fails", docker=True, steps="init,docker")
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")


def test_start_failure_fails_and_stop_is_still_called(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "fail-start", docker=True, steps="init,docker")
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
    assert _calls(tmp_path) == ["start", "stop"]


def test_status_failure_fails_and_stop_is_still_called(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "fail-status", docker=True, steps="init,docker")
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")
    assert _calls(tmp_path) == ["start", "status", "stop"]


def test_a_stack_with_no_containers_while_running_cannot_pass(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "start-no-containers", docker=True, steps="init,docker")
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "no containers" in failing


def test_project_name_comes_from_the_compose_helper_not_a_guess(tmp_path: Path) -> None:
    # The fake docker only knows containers under dango-abcdef12 (from project.id).
    result = run_journey(
        tmp_path, docker=True, env={"FAKE_PROJECT": "dango-someone-else"}, steps="init,docker"
    )
    assert result.returncode == 1
    assert _docker_line(result.stdout).startswith("FAIL docker")


def test_platform_port_still_open_after_stop_fails_the_step(tmp_path: Path) -> None:
    result = run_journey(tmp_path, "stop-leaves-listener", docker=True, steps="init,docker")
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "still open after stop" in failing
    assert reap_orphans(tmp_path)  # positive control: the listener really outlived the stop


def test_containers_of_another_working_dir_are_neither_removed_nor_ignored(
    tmp_path: Path,
) -> None:
    state = tmp_path / "fakestate"
    state.mkdir()
    (state / "containers.txt").write_text("foreign000001\t/some/other/project\n", encoding="utf-8")
    result = run_journey(tmp_path, docker=True, steps="init,docker")
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "remain" in failing
    removed = state / "docker-rm.txt"
    assert not removed.exists() or "foreign000001" not in removed.read_text(encoding="utf-8")
    assert "foreign000001" in (state / "containers.txt").read_text(encoding="utf-8")


def test_volumes_that_existed_before_a_failed_start_are_kept(tmp_path: Path) -> None:
    state = tmp_path / "fakestate"
    state.mkdir()
    (state / "volumes.txt").write_text("earliervolume\n", encoding="utf-8")
    result = run_journey(tmp_path, "fail-start", docker=True, steps="init,docker")
    assert result.returncode == 1
    assert "earliervolume" in (state / "volumes.txt").read_text(encoding="utf-8")


def test_volumes_created_by_this_run_are_removed(tmp_path: Path) -> None:
    state = tmp_path / "fakestate"
    state.mkdir()
    # No volumes before start; the fake dango start creates one for this run's project.
    result = run_journey(tmp_path, "start-creates-volume", docker=True, steps="init,docker")
    assert result.returncode == 0, result.stdout
    assert (state / "volumes.txt").read_text(encoding="utf-8").strip() == ""
    assert "volume run-volume" in (state / "docker-rm.txt").read_text(encoding="utf-8")


def test_platform_is_started_on_the_ports_written_to_project_yml(tmp_path: Path) -> None:
    import yaml

    result = run_journey(tmp_path, docker=True, steps="init,docker")
    assert result.returncode == 0, result.stdout
    project = yaml.safe_load((tmp_path / "work" / ".dango" / "project.yml").read_text("utf-8"))
    ports = [
        project["platform"][k] for k in ("port", "metabase_port", "dbt_docs_port", "marimo_port")
    ]
    assert not set(ports) & {8800, 3000, 8081, 7805}
    started_on = int((tmp_path / "fakestate" / "start-port.txt").read_text(encoding="utf-8"))
    assert started_on == project["platform"]["port"]


def test_a_foreign_container_with_the_same_project_label_does_not_count_as_ours(
    tmp_path: Path,
) -> None:
    state = tmp_path / "fakestate"
    state.mkdir()
    (state / "containers.txt").write_text("foreign000001\t/some/other/project\n", encoding="utf-8")
    result = run_journey(tmp_path, "start-no-containers", docker=True, steps="init,docker")
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "from this workdir" in failing


def test_platform_port_that_never_answers_after_start_fails_the_step(tmp_path: Path) -> None:
    result = run_journey(
        tmp_path, "start-no-listener", docker=True, steps="init,docker", extra=("--up-timeout", "2")
    )
    assert result.returncode == 1
    failing = _docker_line(result.stdout)
    assert failing.startswith("FAIL docker") and "did not answer after start" in failing


def test_a_volume_that_existed_before_a_successful_start_survives_and_the_step_passes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "fakestate"
    state.mkdir()
    (state / "volumes.txt").write_text("preexisting_volume\n", encoding="utf-8")
    result = run_journey(tmp_path, "start-creates-volume", docker=True, steps="init,docker")
    assert result.returncode == 0, result.stdout
    assert _docker_line(result.stdout).startswith("PASS docker")
    assert (state / "volumes.txt").read_text(encoding="utf-8").split() == ["preexisting_volume"]
    removed = (state / "docker-rm.txt").read_text(encoding="utf-8")
    assert "volume run-volume" in removed and "preexisting_volume" not in removed
