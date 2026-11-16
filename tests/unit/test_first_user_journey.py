"""tests/unit/test_first_user_journey.py

scripts/first_user_journey.py walks a first-user session (init, source, sync, count, dbt, web
start/stop/restart, second sync, optional docker) and reports PASS/FAIL/SKIP per step.
"""

import re
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.factories.journey_fakes import (
    lines_of,
    pid_alive,
    reap_orphans,
    run_journey,
    tracked_pids,
)

pytestmark = pytest.mark.unit

_journey = run_journey
CORE = "init,add-source,sync,count,second-sync"


@pytest.fixture(autouse=True)
def _no_orphan_fake_processes(tmp_path: Path) -> Iterator[None]:
    """Fail any test that leaves a fake web/platform process running (and kill it)."""
    yield
    left = reap_orphans(tmp_path)
    assert not left, f"fake processes survived the test: {left}"


@pytest.fixture(scope="module")
def default_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[subprocess.CompletedProcess[str], Path]]:
    """One complete default journey, shared by the tests that only inspect its outcome."""
    base = tmp_path_factory.mktemp("default_run")
    result = run_journey(base)
    yield result, base
    left = reap_orphans(base)
    assert not left, f"fake processes survived the shared run: {left}"


def _first_int(pattern: str, text: str) -> int:
    match = re.search(pattern, text)
    assert match, f"{pattern!r} not found"
    return int(match.group(1))


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _env_seen(base: Path) -> str:
    return (base / "work" / "env-seen.txt").read_text("utf-8")


def test_all_steps_pass_and_docker_is_skipped_by_default(
    default_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, _ = default_run
    assert result.returncode == 0, result.stdout + result.stderr
    for step in ("init", "add-source", "sync", "count", "dbt", "web", "second-sync"):
        assert any(ln.startswith(f"PASS {step}") for ln in result.stdout.splitlines()), step
    assert "SKIP docker: not requested" in result.stdout
    assert not lines_of(result, "FAIL")
    assert "Traceback" not in result.stderr


def test_ports_are_changed_from_the_defaults_and_distinct(
    default_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    import yaml

    _, base = default_run
    data = yaml.safe_load((base / "work" / ".dango" / "project.yml").read_text("utf-8"))
    ports = [data["platform"][k] for k in ("port", "metabase_port", "dbt_docs_port", "marimo_port")]
    assert not set(ports) & {8800, 3000, 8081, 7805}
    assert len(set(ports)) == 4


def test_csv_is_utf8_with_lf_and_non_ascii_round_trips_into_the_log(
    default_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    _, base = default_run
    raw = (base / "work" / "data" / "uploads" / "people.csv").read_bytes()
    assert "Zoë".encode() in raw
    assert b"\r" not in raw
    assert raw.splitlines()[0] == b"id,name,city"
    assert len(raw.splitlines()) == 6
    log = (base / "journey.log").read_text(encoding="utf-8")
    assert "Zoë" in log
    assert "dango" in log and "exit" in log.lower()


def test_web_is_started_twice_and_every_process_and_the_port_are_gone(
    default_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    result, base = default_run
    assert "restart" in next(ln for ln in result.stdout.splitlines() if ln.startswith("PASS web"))
    pids = [int(p) for p in (base / "work" / "web-pids.txt").read_text("utf-8").split()]
    assert len(pids) == 2 and pids[0] != pids[1]
    assert not any(pid_alive(p) for p in pids)
    assert not _port_open(_first_int(r"answered on port (\d+)", result.stdout))


def test_children_get_utf8_stdio_but_keep_platform_file_encodings(
    default_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    _, base = default_run
    seen = _env_seen(base)
    assert "PYTHONIOENCODING=utf-8" in seen
    assert "PYTHONUTF8=<unset>" in seen  # file defaults stay at the platform default
    assert "DO_NOT_TRACK=1" in seen
    assert "PYTHONUNBUFFERED=1" in seen
    assert "DANGO_ADMIN_PASSWORD=<unset>" not in seen
    assert "PYTHON_KEYRING_BACKEND=<unset>" in seen


def test_children_get_plain_terminal_settings(
    default_run: tuple[subprocess.CompletedProcess[str], Path],
) -> None:
    seen = _env_seen(default_run[1])
    assert "NO_COLOR=1" in seen
    assert "TERM=dumb" in seen


def test_utf8_mode_flag_sets_only_python_utf8_mode(tmp_path: Path) -> None:
    result = _journey(tmp_path, extra=("--utf8-mode",), steps="init")
    assert result.returncode == 0, result.stdout
    seen = _env_seen(tmp_path)
    assert "PYTHONUTF8=1" in seen
    assert "PYTHONIOENCODING=utf-8" in seen


def test_legacy_stdio_flag_removes_only_the_stdio_encoding(tmp_path: Path) -> None:
    result = _journey(
        tmp_path, extra=("--legacy-stdio",), env={"PYTHONIOENCODING": "cp1252"}, steps="init"
    )
    assert result.returncode == 0, result.stdout
    seen = _env_seen(tmp_path)
    assert "PYTHONIOENCODING=<unset>" in seen
    assert "PYTHONUTF8=<unset>" in seen


def test_isolate_keyring_flag_sets_the_fail_backend(tmp_path: Path) -> None:
    result = _journey(tmp_path, extra=("--isolate-keyring",), steps="init")
    assert result.returncode == 0, result.stdout
    seen = _env_seen(tmp_path)
    assert "PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring" in seen


def test_failed_step_skips_dependents_and_exits_one(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="fail-sync")
    assert result.returncode == 1
    assert "FAIL sync: dango sync --yes: exit code 1" in result.stdout
    assert "SKIP count: depends on sync" in result.stdout
    assert "SKIP dbt: depends on sync" in result.stdout
    assert "SKIP second-sync: depends on" in result.stdout
    assert any(ln.startswith("PASS web") for ln in result.stdout.splitlines())
    assert "boom" in result.stdout  # tail of the failing step


def test_failed_init_skips_everything_that_needs_a_project(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="fail-init")
    assert result.returncode == 1
    assert "FAIL init" in result.stdout
    assert "SKIP add-source: depends on init" in result.stdout
    assert "SKIP web: depends on init" in result.stdout
    assert not any(ln.startswith("PASS") for ln in result.stdout.splitlines())


def test_missing_dango_executable_fails_init_without_traceback(tmp_path: Path) -> None:
    result = _journey(tmp_path, dango=tmp_path / "nope" / "dango")
    assert result.returncode == 1
    assert "FAIL init" in result.stdout
    assert "SKIP sync" in result.stdout
    assert "Traceback" not in result.stderr


def test_web_step_fails_when_the_process_exits_before_it_is_up(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="web-exit-early", steps="init,web")
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL web"))
    assert "process exited with code 3" in failing
    assert "web: cannot bind, giving up" in result.stdout


def test_web_child_that_ignores_terminate_is_killed(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="web-ignore-term", steps="init,web")
    assert "Traceback" not in result.stderr
    pids = [int(p) for p in (tmp_path / "work" / "web-pids.txt").read_text("utf-8").split()]
    assert pids
    assert not any(pid_alive(p) for p in pids)
    assert result.returncode == 0, result.stdout


def test_second_sync_that_changes_the_count_fails_idempotency(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="grow-on-second-sync", steps=CORE)
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL second-sync"))
    assert "5" in failing and "6" in failing


def test_step_timeout_is_reported_and_does_not_hang(tmp_path: Path) -> None:
    result = _journey(
        tmp_path,
        mode="slow-sync",
        extra=("--step-timeout", "2"),
        steps="init,add-source,sync,count",
    )
    assert result.returncode == 1
    assert "FAIL sync: timed out after 2s" in result.stdout
    assert "SKIP count: depends on sync" in result.stdout


def test_non_ascii_output_does_not_crash_the_reporter_under_a_cp1252_stdout(
    tmp_path: Path,
) -> None:
    env = {"PYTHONIOENCODING": "cp1252"}
    result = _journey(
        tmp_path, mode="fail-sync", env=env, extra=("--legacy-stdio",), steps="init,add-source,sync"
    )
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "FAIL sync" in result.stdout
    assert result.stdout.isascii()
    assert "boom" in result.stdout
    log = (tmp_path / "journey.log").read_text(encoding="utf-8")
    assert "boom" in log


def test_count_step_fails_when_the_table_has_too_few_rows(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="short-first-sync", steps=CORE)
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL count"))
    assert "4 rows" in failing and "expected 5" in failing
    assert "SKIP second-sync: depends on count" in result.stdout


def test_count_step_fails_when_there_is_no_people_table(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="no-table", steps=CORE)
    assert result.returncode == 1
    assert "FAIL count: no table containing 'people'" in result.stdout
    assert not any(ln.startswith("PASS count") for ln in result.stdout.splitlines())


def test_count_step_fails_when_the_non_ascii_value_was_mangled(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="mangled-name", steps=CORE)
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL count"))
    assert "Zo\\xeb" in failing  # the expected value, ASCII-escaped in the report
    assert result.stdout.isascii()


def test_web_step_fails_when_a_listener_keeps_the_port_after_the_stop(tmp_path: Path) -> None:
    result = _journey(
        tmp_path, mode="web-orphan-listener", extra=("--port-free-timeout", "1"), steps="init,web"
    )
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL web"))
    assert "still in use" in failing
    # Positive control: the surviving listener really existed (the fixture would also catch it).
    assert reap_orphans(tmp_path)


def test_web_child_is_stopped_and_the_port_freed_when_the_step_raises(tmp_path: Path) -> None:
    result = _journey(tmp_path, mode="web-garbage", steps="init,web")
    assert result.returncode == 1
    assert "FAIL web" in result.stdout
    pids = tracked_pids(tmp_path)
    assert pids
    assert not any(pid_alive(p) for p in pids)
    log = (tmp_path / "journey.log").read_text(encoding="utf-8")
    assert not _port_open(_first_int(r"--port (\d+)", log))
    # The web child's own output reaches the log even though the step raised.
    assert "fake web starting" in log


def test_final_log_line_is_ascii_even_for_a_log_path_outside_cp1252(tmp_path: Path) -> None:
    env = {"PYTHONIOENCODING": "cp1252"}
    result = _journey(tmp_path, env=env, log_name="\u65e5\u672c.log", steps="init")
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert result.stdout.isascii()
    assert any(ln.startswith("log: ") for ln in result.stdout.splitlines())


def test_total_budget_caps_step_timeouts_and_stops_later_steps(tmp_path: Path) -> None:
    started = time.monotonic()
    result = _journey(
        tmp_path,
        mode="slow-sync",
        extra=("--budget", "5"),
        steps="init,add-source,sync,count,web",
    )
    assert time.monotonic() - started < 25
    assert result.returncode == 1
    assert re.search(r"FAIL sync: timed out after [0-5](\.\d+)?s", result.stdout), result.stdout
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL web"))
    assert "budget" in failing
    assert "SKIP count: depends on sync" in result.stdout
