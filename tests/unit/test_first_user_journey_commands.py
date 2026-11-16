"""tests/unit/test_first_user_journey_commands.py

Table-driven check of scripts/first_user_journey.py: every command the journey runs (dango,
the venv python helper snippets, docker) is made to fail, or to hang, on its own; the journey
must exit 1 with exactly the right step FAIL and a message naming the failing command.
"""

import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from tests.factories.journey_fakes import Outcome, lines_of, run_scenarios

pytestmark = pytest.mark.unit

CORE = "init,add-source,sync,count,dbt,second-sync"

# (id, injected command key, nth call, steps, extra fake mode, FAIL step, expected message parts)
JOURNEY_ROWS: list[tuple[str, str, int, str, str, str, tuple[str, ...]]] = [
    ("init", "init", 1, "init", "", "init", ("dango init --skip-wizard: exit code 1",)),
    ("set-ports", "py-set-ports", 1, "init", "", "init", ("setting non-default ports",)),
    (
        "source-list",
        "source-list",
        1,
        "init,add-source",
        "",
        "add-source",
        ("dango source list: exit code 1",),
    ),
    (
        "validate",
        "validate",
        1,
        "init,add-source",
        "",
        "add-source",
        ("dango validate: exit code 1",),
    ),
    ("sync-1", "sync", 1, "init,add-source,sync", "", "sync", ("dango sync --yes: exit code 1",)),
    (
        "count-1",
        "py-count",
        1,
        "init,add-source,sync,count",
        "",
        "count",
        ("reading the warehouse",),
    ),
    ("dbt", "run", 1, "init,add-source,sync,dbt", "", "dbt", ("dango run: exit code 1",)),
    ("sync-2", "sync", 2, CORE, "", "second-sync", ("dango sync --yes: exit code 1",)),
    ("count-2", "py-count", 2, CORE, "", "second-sync", ("reading the warehouse",)),
]

DOCKER_ROWS: list[tuple[str, str, int, str, tuple[str, ...]]] = [
    ("project-info", "py-project-info", 1, "", ("could not read the Compose project name",)),
    ("volume-ls-before-start", "d-volume-ls", 1, "", ("docker volume ls", "exited 1")),
    ("start", "start", 1, "", ("start: exit code 1",)),
    ("ps-while-up", "d-ps", 1, "", ("docker ps", "exited 1")),
    ("status", "status", 1, "", ("status: exit code 1",)),
    ("stop", "stop", 1, "", ("stop: exit code 1",)),
    ("ps-after-stop", "d-ps", 2, "", ("docker ps", "exited 1")),
    ("ps-in-cleanup", "d-ps", 3, "", ("docker cleanup incomplete", "docker ps")),
    (
        "volume-ls-in-cleanup",
        "d-volume-ls",
        2,
        "",
        ("docker cleanup incomplete", "docker volume ls"),
    ),
    (
        "volume-rm",
        "d-volume-rm",
        1,
        "start-creates-volume",
        ("docker cleanup incomplete", "volume rm"),
    ),
    ("container-rm", "d-rm", 1, "leftover", ("remain", "docker rm")),
]

# Commands that can hang; each is made to outlive a 4 second step timeout.
HANG_ROWS = [
    row
    for row in JOURNEY_ROWS
    if row[0] in {"init", "set-ports", "source-list", "validate", "dbt", "sync-2"}
]


Run = subprocess.CompletedProcess[str]


def _scenarios() -> dict[str, dict[str, Any]]:
    """Every scenario as id -> (kind, run_journey kwargs); all of them run once, in parallel."""
    out: dict[str, dict[str, Any]] = {}
    for rid, key, nth, steps, mode, _, _ in JOURNEY_ROWS:
        out[f"fail-{rid}"] = {"mode": mode, "steps": steps, "env": {"FAKE_FAIL": f"{key}:{nth}"}}
    for rid, key, nth, steps, mode, _, _ in HANG_ROWS:
        out[f"hang-{rid}"] = {
            "mode": mode,
            "steps": steps,
            "env": {"FAKE_HANG": f"{key}:{nth}"},
            "extra": ("--step-timeout", "4"),
        }
    for rid, key, nth, mode, _ in DOCKER_ROWS:
        out[f"docker-{rid}"] = {
            "mode": mode,
            "steps": "init,docker",
            "docker": True,
            "env": {"FAKE_FAIL": f"{key}:{nth}"},
        }
    return out


@pytest.fixture(scope="module")
def runs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Outcome]]:
    """Run every scenario once (in parallel, each in its own directory)."""
    yield from run_scenarios(tmp_path_factory, _scenarios())


def _only_failure(result: Run, step: str) -> str:
    fails = lines_of(result, "FAIL")
    assert len(fails) == 1, f"expected exactly one FAIL line, got {fails}\n{result.stdout}"
    assert fails[0].startswith(f"FAIL {step}"), f"wrong step failed: {fails[0]}"
    return fails[0]


@pytest.mark.parametrize("row", JOURNEY_ROWS, ids=[r[0] for r in JOURNEY_ROWS])
def test_a_failing_command_fails_exactly_its_step(
    runs: dict[str, Outcome], row: tuple[str, str, int, str, str, str, tuple[str, ...]]
) -> None:
    result = runs[f"fail-{row[0]}"][0]
    assert result.returncode == 1, result.stdout
    line = _only_failure(result, row[5])
    for part in row[6]:
        assert part in line, f"{part!r} not in {line!r}"


@pytest.mark.parametrize("row", HANG_ROWS, ids=[r[0] for r in HANG_ROWS])
def test_a_hanging_command_times_out_in_exactly_its_step(
    runs: dict[str, Outcome], row: tuple[str, str, int, str, str, str, tuple[str, ...]]
) -> None:
    result = runs[f"hang-{row[0]}"][0]
    assert result.returncode == 1, result.stdout
    assert "timed out after" in _only_failure(result, row[5])


@pytest.mark.parametrize("row", DOCKER_ROWS, ids=[r[0] for r in DOCKER_ROWS])
def test_a_failing_docker_step_command_fails_the_docker_step(
    runs: dict[str, Outcome], row: tuple[str, str, int, str, tuple[str, ...]]
) -> None:
    result = runs[f"docker-{row[0]}"][0]
    assert result.returncode == 1, result.stdout
    line = _only_failure(result, "docker")
    for part in row[4]:
        assert part in line, f"{part!r} not in {line!r}"
