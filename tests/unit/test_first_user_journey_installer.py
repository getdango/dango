"""tests/unit/test_first_user_journey_installer.py

scripts/first_user_journey.py --installer: run the installer with the built wheel and the
non-interactive answers into a clean directory, then continue the journey with the dango it
installed (not a dango the harness built). A fake installer stands in for install.sh.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.factories.journey_fakes import reap_orphans, run_journey
from tests.factories.journey_installer_fakes import make_fake_installer

pytestmark = pytest.mark.unit

WHEEL = "/dist/getdango-1.0.15-py3-none-any.whl"
URL_SPEC = "https://example.invalid/dist/getdango-1.0.15-py3-none-any.whl"


@pytest.fixture(autouse=True)
def _no_orphan_fake_processes(tmp_path: Path) -> Iterator[None]:
    yield
    left = reap_orphans(tmp_path)
    assert not left, f"fake processes survived the test: {left}"


def _installer_journey(
    tmp_path: Path,
    *,
    spec: str = WHEEL,
    env: dict[str, str] | None = None,
    steps: str | None = None,
    relative: bool = False,
    extra: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    script = make_fake_installer(tmp_path / "installer-src")
    shown = script.relative_to(tmp_path) if relative else script
    return run_journey(
        tmp_path,
        env={"FAKE_BIN": str(tmp_path / "fakebin"), **(env or {})},
        extra=("--installer", "--spec", spec, "--installer-script", str(shown), *extra),
        no_dango=True,
        steps=steps,
        relative=relative,
    )


def _installer_dir(tmp_path: Path) -> Path:
    return tmp_path / "work-installer"


def test_installer_step_passes_then_the_usual_journey_runs(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    for step in ("installer", "init", "add-source", "sync", "count", "dbt", "web", "second-sync"):
        assert any(ln.startswith(f"PASS {step}") for ln in lines), (step, result.stdout)
    assert lines.index(next(ln for ln in lines if ln.startswith("PASS installer"))) < lines.index(
        next(ln for ln in lines if ln.startswith("PASS init"))
    )


def test_installer_receives_the_spec_and_the_noninteractive_answers(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = (_installer_dir(tmp_path) / "install-env.txt").read_text("utf-8").splitlines()
    assert f"DANGO_INSTALL_SPEC={WHEEL}" in seen
    for answer in (
        "DANGO_INSTALL_NONINTERACTIVE=1",
        "DANGO_INSTALL_DOCKER_CONTINUE=y",
        "DANGO_INSTALL_MODE=1",
        "DANGO_INSTALL_SETUP=1",
    ):
        assert answer in seen, answer


def test_installer_runs_in_a_clean_directory_apart_from_the_workdir(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    cwd = (_installer_dir(tmp_path) / "install-cwd.txt").read_text("utf-8").strip()
    assert Path(cwd).resolve() == _installer_dir(tmp_path).resolve()
    assert not (tmp_path / "work" / "install-env.txt").exists()
    assert not (tmp_path / "work" / "venv").exists()


def test_journey_uses_the_dango_and_python_the_installer_put_in_its_venv(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    log = (tmp_path / "journey.log").read_text("utf-8")
    venv_bin = _installer_dir(tmp_path) / "venv" / "bin"
    assert f"$ {venv_bin / 'dango'} init --skip-wizard" in log
    assert f"$ {venv_bin / 'python'} -c" in log
    assert str(tmp_path / "fakebin" / "dango") not in log
    assert str(tmp_path / "fakebin" / "python") not in log


def test_failed_installer_fails_the_step_and_skips_the_rest(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path, env={"FAKE_INSTALLER_FAIL": "1"})
    assert result.returncode == 1
    assert "FAIL installer" in result.stdout
    assert "installer: boom" in result.stdout
    assert "SKIP init: depends on installer" in result.stdout
    assert "SKIP sync: depends on" in result.stdout
    assert not any(ln.startswith("PASS") for ln in result.stdout.splitlines())
    assert "Traceback" not in result.stderr


def test_error_text_in_a_successful_installer_run_fails_the_step(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path, env={"FAKE_INSTALLER_PIP_ERROR": "1"})
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL installer"))
    assert "ERROR:" in failing
    assert "SKIP init: depends on installer" in result.stdout


def test_installer_that_leaves_no_dango_fails_the_step(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path, env={"FAKE_INSTALLER_NO_DANGO": "1"})
    assert result.returncode == 1
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL installer"))
    assert "dango" in failing
    assert "SKIP init: depends on installer" in result.stdout


def test_relative_wheel_path_is_made_absolute_for_the_installer(tmp_path: Path) -> None:
    (tmp_path / "pkg.whl").write_bytes(b"")
    result = _installer_journey(tmp_path, spec="pkg.whl", relative=True, steps="installer")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = (_installer_dir(tmp_path) / "install-env.txt").read_text("utf-8").splitlines()
    assert f"DANGO_INSTALL_SPEC={tmp_path / 'pkg.whl'}" in seen


def test_non_path_spec_is_passed_through_unchanged(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path, spec="getdango==1.0.15", steps="installer")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = (_installer_dir(tmp_path) / "install-env.txt").read_text("utf-8").splitlines()
    assert "DANGO_INSTALL_SPEC=getdango==1.0.15" in seen


def test_installer_requires_a_spec(tmp_path: Path) -> None:
    result = run_journey(tmp_path, extra=("--installer",), no_dango=True)
    assert result.returncode == 2
    assert "--spec" in result.stderr


def test_installer_and_dango_are_mutually_exclusive(tmp_path: Path) -> None:
    result = run_journey(tmp_path, extra=("--installer", "--spec", WHEEL))
    assert result.returncode == 2
    assert "drop --dango" in result.stderr


def test_default_journey_has_no_installer_step(tmp_path: Path) -> None:
    result = run_journey(tmp_path, steps="init")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "installer" not in result.stdout
    assert not (tmp_path / "work-installer").exists()


def test_powershell_installer_runs_as_utf8_text_like_the_documented_one_liner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    sys.modules.pop("journey_installer", None)
    import journey_installer

    argv = journey_installer.installer_command(Path("C:/co/install.ps1"))
    # Run the way the documented one-liner runs it: the script text through iex, no -File.
    assert "-File" not in argv
    # The one-liner passes no execution-policy option, so neither does the journey.
    assert "-ExecutionPolicy" not in argv
    command = argv[argv.index("-Command") + 1]
    assert "ReadAllText" in command and "UTF8" in command and "install.ps1" in command
    # the installer's own exit code, not just "the last command succeeded"
    assert command.endswith("exit $LASTEXITCODE")
    assert journey_installer.installer_command(Path("/co/install.sh")) == ["bash", "/co/install.sh"]


# --- the installed getdango must come from the spec under test ----------------------------------


def test_installer_that_ignores_a_wheel_spec_fails_the_journey(tmp_path: Path) -> None:
    result = _installer_journey(tmp_path, env={"FAKE_INSTALLER_IGNORE_SPEC": "1"})
    assert result.returncode == 1, result.stdout
    failing = next(ln for ln in result.stdout.splitlines() if ln.startswith("FAIL installer"))
    assert "does not come from" in failing and WHEEL in failing
    assert "SKIP init: depends on installer" in result.stdout
    assert not any(ln.startswith("PASS") for ln in result.stdout.splitlines())


def test_installer_that_ignores_a_version_spec_fails_the_journey(tmp_path: Path) -> None:
    result = _installer_journey(
        tmp_path, spec="getdango==1.0.15", env={"FAKE_INSTALLER_IGNORE_SPEC": "1"}
    )
    assert result.returncode == 1, result.stdout
    assert "does not come from getdango==1.0.15" in result.stdout


def test_installer_that_ignores_a_url_spec_fails_the_journey(tmp_path: Path) -> None:
    result = _installer_journey(
        tmp_path, spec=URL_SPEC, env={"FAKE_INSTALLER_IGNORE_SPEC": "1"}, steps="installer"
    )
    assert result.returncode == 1, result.stdout
    assert f"does not come from {URL_SPEC}" in result.stdout


@pytest.mark.parametrize("spec", [WHEEL, "getdango==1.0.15", URL_SPEC])
def test_installer_that_honours_the_spec_passes_and_says_what_was_installed(
    tmp_path: Path, spec: str
) -> None:
    result = _installer_journey(tmp_path, spec=spec, steps="installer")
    assert result.returncode == 0, result.stdout + result.stderr
    passing = next(ln for ln in result.stdout.splitlines() if ln.startswith("PASS installer"))
    assert "getdango 1.0.15" in passing and spec in passing


def test_wheel_path_with_spaces_is_passed_and_verified(tmp_path: Path) -> None:
    spec = str(tmp_path / "my dist" / "getdango-1.0.15-py3-none-any.whl")
    result = _installer_journey(tmp_path, spec=spec, steps="installer")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = (_installer_dir(tmp_path) / "install-env.txt").read_text("utf-8").splitlines()
    assert f"DANGO_INSTALL_SPEC={spec}" in seen


@pytest.mark.parametrize("key", ["PSExecutionPolicyPreference", "PSEXECUTIONPOLICYPREFERENCE"])
def test_installer_does_not_inherit_a_process_execution_policy(tmp_path: Path, key: str) -> None:
    # Windows upper-cases the names in os.environ, so both spellings must be dropped.
    result = _installer_journey(tmp_path, env={key: "Bypass"}, steps="installer")
    assert result.returncode == 0, result.stdout + result.stderr
    seen = (_installer_dir(tmp_path) / "install-ps-policy.txt").read_text("utf-8")
    assert seen.strip() == "<unset>"
    env_seen = (_installer_dir(tmp_path) / "install-env.txt").read_text("utf-8")
    assert f"DANGO_INSTALL_SPEC={WHEEL}" in env_seen  # the rest of the environment is kept


@pytest.mark.parametrize("spec", ["getdango", "getdango>=1.0", "getdango[extra]"])
def test_unverifiable_specs_pass_but_say_the_source_was_not_verified(
    tmp_path: Path, spec: str
) -> None:
    result = _installer_journey(tmp_path, spec=spec, steps="installer")
    assert result.returncode == 0, result.stdout + result.stderr
    passing = next(ln for ln in result.stdout.splitlines() if ln.startswith("PASS installer"))
    assert "(source not verified)" in passing


@pytest.mark.parametrize("spec", [WHEEL, "getdango==1.0.15", URL_SPEC])
def test_verified_specs_do_not_claim_the_source_was_not_verified(tmp_path: Path, spec: str) -> None:
    result = _installer_journey(tmp_path, spec=spec, steps="installer")
    passing = next(ln for ln in result.stdout.splitlines() if ln.startswith("PASS installer"))
    assert "not verified" not in passing
