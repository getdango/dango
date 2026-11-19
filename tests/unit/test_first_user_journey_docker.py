"""tests/unit/test_first_user_journey_docker.py

The opt-in docker step of scripts/first_user_journey.py (start, status, stop, leftover check and
cleanup scoping), driven with a fake dango and a fake docker on PATH. All scenarios run once, in
parallel; each test inspects its own scenario's outcome.
"""

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.factories.journey_fakes import (
    Outcome,
    pid_alive,
    run_scenarios,
    tracked_pids,
)

pytestmark = pytest.mark.unit

FOREIGN = "foreign000001\t/some/other/project\n"
BASE = {"docker": True, "steps": "init,docker"}

SCENARIOS: dict[str, dict[str, object]] = {
    "pass": {},
    "leftover": {"mode": "leftover"},
    "ps-fails": {"mode": "ps-fails"},
    "volume-ls-fails": {"mode": "volume-ls-fails"},
    "fail-start": {"mode": "fail-start"},
    "fail-status": {"mode": "fail-status"},
    "no-containers": {"mode": "start-no-containers"},
    "wrong-project": {"env": {"FAKE_PROJECT": "dango-someone-else"}},
    "listener-left": {"mode": "stop-leaves-listener"},
    "foreign-container": {"seed": {"containers.txt": FOREIGN}},
    "foreign-and-none-of-ours": {
        "mode": "start-no-containers",
        "seed": {"containers.txt": FOREIGN},
    },
    "volume-kept-failed-start": {"mode": "fail-start", "seed": {"volumes.txt": "earlier\n"}},
    "volume-created": {"mode": "start-creates-volume"},
    "volume-preexisting": {"mode": "start-creates-volume", "seed": {"volumes.txt": "pre\n"}},
    "no-listener": {"mode": "start-no-listener", "extra": ("--up-timeout", "2")},
    "init-fails": {"mode": "fail-init"},
    "bad-bytes": {"mode": "leftover,start-creates-volume,docker-bad-bytes"},
}


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Outcome]]:
    """Run every docker scenario once, in parallel."""
    scenarios = {k: {**BASE, **v} for k, v in SCENARIOS.items()}
    yield from run_scenarios(
        tmp_path_factory, scenarios, expect_survivors=frozenset({"listener-left"})
    )


def _docker_line(runs: dict[str, Outcome], name: str) -> str:
    stdout = runs[name][0].stdout
    return next(ln for ln in stdout.splitlines() if re.match(r"(PASS|FAIL|SKIP) docker", ln))


def _state(runs: dict[str, Outcome], name: str) -> Path:
    return runs[name][1] / "fakestate"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_docker_step_passes_and_leaves_nothing_behind(runs: dict[str, Outcome]) -> None:
    assert runs["pass"][0].returncode == 0, runs["pass"][0].stdout
    assert _docker_line(runs, "pass").startswith("PASS docker")
    assert _read(_state(runs, "pass") / "docker-calls.txt").split() == ["start", "status", "stop"]
    assert _read(_state(runs, "pass") / "containers.txt").strip() == ""


def test_containers_left_after_stop_fail_the_step_and_are_removed(
    runs: dict[str, Outcome],
) -> None:
    line = _docker_line(runs, "leftover")
    assert line.startswith("FAIL docker") and "remain" in line
    assert "abc123def456" in _read(_state(runs, "leftover") / "docker-rm.txt")


def test_docker_ps_failure_is_a_fail_not_zero_containers(runs: dict[str, Outcome]) -> None:
    assert runs["ps-fails"][0].returncode == 1
    assert _docker_line(runs, "ps-fails").startswith("FAIL docker")


def test_docker_volume_ls_failure_is_a_fail(runs: dict[str, Outcome]) -> None:
    assert runs["volume-ls-fails"][0].returncode == 1
    assert _docker_line(runs, "volume-ls-fails").startswith("FAIL docker")


def test_start_failure_fails_and_stop_is_still_called(runs: dict[str, Outcome]) -> None:
    assert _docker_line(runs, "fail-start").startswith("FAIL docker")
    assert _read(_state(runs, "fail-start") / "docker-calls.txt").split() == ["start", "stop"]


def test_status_failure_fails_and_stop_is_still_called(runs: dict[str, Outcome]) -> None:
    assert _docker_line(runs, "fail-status").startswith("FAIL docker")
    calls = _read(_state(runs, "fail-status") / "docker-calls.txt").split()
    assert calls == ["start", "status", "stop"]


def test_a_stack_with_no_containers_while_running_cannot_pass(runs: dict[str, Outcome]) -> None:
    line = _docker_line(runs, "no-containers")
    assert line.startswith("FAIL docker") and "no containers" in line


def test_project_name_comes_from_the_compose_helper_not_a_guess(runs: dict[str, Outcome]) -> None:
    # The fake docker only knows containers under dango-abcdef12 (from project.id).
    assert _docker_line(runs, "wrong-project").startswith("FAIL docker")


def test_platform_port_still_open_after_stop_fails_the_step(runs: dict[str, Outcome]) -> None:
    line = _docker_line(runs, "listener-left")
    assert line.startswith("FAIL docker") and "still open after stop" in line
    # Positive control: the listener really outlived the stop.
    assert any(pid_alive(p) for p in tracked_pids(runs["listener-left"][1]))


def test_containers_of_another_working_dir_are_neither_removed_nor_ignored(
    runs: dict[str, Outcome],
) -> None:
    line = _docker_line(runs, "foreign-container")
    assert line.startswith("FAIL docker") and "remain" in line
    state = _state(runs, "foreign-container")
    assert "foreign000001" not in _read(state / "docker-rm.txt")
    assert "foreign000001" in _read(state / "containers.txt")


def test_a_foreign_container_with_the_same_project_label_does_not_count_as_ours(
    runs: dict[str, Outcome],
) -> None:
    line = _docker_line(runs, "foreign-and-none-of-ours")
    assert line.startswith("FAIL docker") and "from this workdir" in line


def test_volumes_that_existed_before_a_failed_start_are_kept(runs: dict[str, Outcome]) -> None:
    assert "earlier" in _read(_state(runs, "volume-kept-failed-start") / "volumes.txt")


def test_volumes_created_by_this_run_are_removed(runs: dict[str, Outcome]) -> None:
    state = _state(runs, "volume-created")
    assert runs["volume-created"][0].returncode == 0
    assert _read(state / "volumes.txt").strip() == ""
    assert "volume run-volume" in _read(state / "docker-rm.txt")


def test_a_volume_that_existed_before_a_successful_start_survives_and_the_step_passes(
    runs: dict[str, Outcome],
) -> None:
    assert _docker_line(runs, "volume-preexisting").startswith("PASS docker")
    state = _state(runs, "volume-preexisting")
    assert _read(state / "volumes.txt").split() == ["pre"]
    removed = _read(state / "docker-rm.txt")
    assert "volume run-volume" in removed and "pre\n" not in removed.replace("run-volume", "")


def test_platform_is_started_on_the_ports_written_to_project_yml(runs: dict[str, Outcome]) -> None:
    work, state = runs["pass"][1] / "work", _state(runs, "pass")
    project = yaml.safe_load((work / ".dango" / "project.yml").read_text("utf-8"))
    ports = [
        project["platform"][k] for k in ("port", "metabase_port", "dbt_docs_port", "marimo_port")
    ]
    assert not set(ports) & {8800, 3000, 8081, 7805}
    assert int(_read(state / "start-port.txt")) == project["platform"]["port"]


def test_platform_port_that_never_answers_after_start_fails_the_step(
    runs: dict[str, Outcome],
) -> None:
    line = _docker_line(runs, "no-listener")
    assert line.startswith("FAIL docker") and "did not answer after start" in line


def test_docker_is_skipped_not_failed_when_init_failed(runs: dict[str, Outcome]) -> None:
    result = runs["init-fails"][0]
    assert result.returncode == 1
    assert "SKIP docker: depends on init" in result.stdout
    assert "FAIL docker" not in result.stdout


def test_undecodable_docker_output_does_not_abort_the_cleanup(runs: dict[str, Outcome]) -> None:
    result, work = runs["bad-bytes"]
    assert "UnicodeDecodeError" not in result.stdout + result.stderr
    state = work / "fakestate"
    assert "abc123def456" in _read(state / "docker-rm.txt")
    # The container removal printed bytes that are not UTF-8; the volume removal still ran.
    assert _read(state / "volumes.txt").strip() == ""


def test_scenarios_never_share_a_port() -> None:
    from tests.factories.journey_fakes import allocate_scenario_ports

    names = [f"s{i}" for i in range(40)]
    ports = allocate_scenario_ports(names)
    flat = [p for name in names for p in ports[name]]
    assert all(len(ports[name]) == 5 for name in names)
    assert len(flat) == len(set(flat)) == 200
