"""tests/unit/test_first_user_journey_diagnostics.py

The journey names the validate checks that failed or warned, and its fake docker and fake dango
are strict about the arguments the journey must use (a missing `-a` or a made-up subcommand).
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests.factories.journey_fake_dango import FAKE_DANGO
from tests.factories.journey_fakes import FAKE_DOCKER, make_fake_exe, run_journey

pytestmark = pytest.mark.unit


def test_failed_validate_names_the_failing_and_warning_checks(tmp_path: Path) -> None:
    result = run_journey(tmp_path, mode="validate-rows", steps="init,add-source")
    assert result.returncode == 1
    fail_line = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL add-source"))
    assert "dbt" in fail_line
    assert "timed out after 5 seconds" in fail_line
    # The flagged rows are far above the last 15 lines, so the plain tail cannot show them.
    assert "dbt" in result.stdout.split("--- flagged checks ---")[1]
    assert "git not found" in result.stdout.split("--- flagged checks ---")[1]


def test_flagged_checks_are_ascii_safe_and_passing_rows_are_not_listed(tmp_path: Path) -> None:
    result = run_journey(tmp_path, mode="validate-rows", steps="init,add-source")
    flagged = result.stdout.split("--- flagged checks ---")[1].split("---")[0]
    assert flagged.isascii()
    assert "DuckDB" not in flagged
    assert "dir3" not in flagged


def test_the_log_keeps_the_full_validate_output(tmp_path: Path) -> None:
    run_journey(tmp_path, mode="validate-rows", steps="init,add-source")
    log = (tmp_path / "journey.log").read_text("utf-8")
    assert "dbt         ✗ FAIL" in log
    assert "Passed: 25  Warnings: 1  Failed: 1" in log


def test_a_plain_validate_failure_still_reports_the_exit_code(tmp_path: Path) -> None:
    result = run_journey(tmp_path, mode="fail-validate", steps="init,add-source")
    assert "FAIL add-source: dango validate: exit code 1" in result.stdout


def _real_table() -> str:
    """The table validate itself prints (its own _display_results) at the CI console width."""
    import io

    from rich.console import Console

    import dango.cli.validate as validate_module

    results = validate_module.ValidationResult
    validator = validate_module.ProjectValidator(Path("."))
    validator.results = [
        results("Package: DuckDB", "pass"),
        results("CLI: dbt", "fail", "dbt --version timed out after 30 seconds. " + "slow " * 20),
        results("Security: .env", "warn", "Sensitive file tracked by git: .env"),
    ]
    buffer = io.StringIO()
    original = validate_module.console
    validate_module.console = Console(file=buffer, width=80, force_terminal=False)
    try:
        validator._display_results()
    finally:
        validate_module.console = original
    return buffer.getvalue()


def test_flagged_rows_parse_the_real_rich_table_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from journey_support import flagged_rows

    rows = flagged_rows(_real_table())
    assert [status for status, _ in rows] == ["FAIL", "WARN"]
    assert rows[0][1].startswith("CLI/dbt: dbt --version timed out after 30 seconds")
    assert rows[1][1] == "Security/.env: Sensitive file tracked by git: .env"


def test_a_flagged_row_without_the_glyph_is_still_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from journey_support import flagged_rows

    rows = flagged_rows("CLI\n dbt    FAIL  broken\n git    WARN  old\n")
    assert rows == [("FAIL", "CLI/dbt: broken"), ("WARN", "CLI/git: old")]


def test_flagged_lines_never_look_like_step_lines(tmp_path: Path) -> None:
    result = run_journey(tmp_path, mode="validate-rows", steps="init,add-source")
    step_lines = [ln for ln in result.stdout.splitlines() if re.match(r"^(PASS|FAIL|SKIP) ", ln)]
    assert step_lines == [
        next(ln for ln in step_lines if ln.startswith("PASS init")),
        next(ln for ln in step_lines if ln.startswith("FAIL add-source")),
    ]
    assert "CLI/dbt" in result.stdout  # the flagged rows are still printed, indented


def test_children_get_a_wide_console_so_details_are_not_wrapped(tmp_path: Path) -> None:
    run_journey(tmp_path, steps="init")
    assert "COLUMNS=200" in (tmp_path / "work" / "env-seen.txt").read_text("utf-8")


def _fake_docker(tmp_path: Path) -> Path:
    return make_fake_exe(tmp_path / "bin", "docker", FAKE_DOCKER)


def _docker(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    env = {"FAKE_STATE_DIR": str(state), "FAKE_PROJECT": "p", "PATH": "/usr/bin:/bin"}
    return subprocess.run(
        [str(_fake_docker(tmp_path)), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_fake_docker_rejects_ps_without_dash_a(tmp_path: Path) -> None:
    result = _docker(tmp_path, "ps", "--filter", "label=com.docker.compose.project=p")
    assert result.returncode != 0
    assert "-a" in result.stderr


def test_fake_docker_accepts_ps_with_dash_a(tmp_path: Path) -> None:
    result = _docker(tmp_path, "ps", "-a", "--filter", "label=com.docker.compose.project=p")
    assert result.returncode == 0


def test_fake_dango_rejects_an_unknown_source_subcommand(tmp_path: Path) -> None:
    result = run_journey(tmp_path, mode="", steps="init,add-source")
    assert result.returncode == 0
    exe = make_fake_exe(tmp_path / "bin2", "dango", FAKE_DANGO)
    state = tmp_path / "fakestate"
    work = tmp_path / "work"
    env = {"FAKE_STATE_DIR": str(state), "PATH": "/usr/bin:/bin"}
    bad = subprocess.run(
        [str(exe), "source", "bogus"],
        cwd=work,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert bad.returncode != 0
    good = subprocess.run(
        [str(exe), "source", "list"],
        cwd=work,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert good.returncode == 0
