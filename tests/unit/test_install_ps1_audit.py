"""tests/unit/test_install_ps1_audit.py

install.ps1 audit fixes, checked structurally on any OS (PowerShell is not run here; the Windows
platform-smoke jobs run the real script): ASCII-only text, the effective execution policy, no
`exit` that would close an `irm | iex` window, and the clear failure and next-step messages.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "install.ps1").read_text(encoding="utf-8")
CODE = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))


def _function(name: str) -> str:
    start = TEXT.index(f"function {name}")
    return TEXT[start : TEXT.index("\n}\n", start)]


# --- 1. Windows PowerShell 5.1 reads a BOM-less file as ANSI ------------------------------------


def _shipped_scripts() -> list[Path]:
    return sorted([*ROOT.glob("*.ps1"), *(ROOT / "scripts").rglob("*.ps1")])


@pytest.mark.parametrize("path", _shipped_scripts(), ids=lambda p: p.name)
def test_every_shipped_powershell_script_is_ascii_or_has_a_bom(path: Path) -> None:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    assert not bad, f"{path.name} has non-ASCII bytes at {bad[:5]} and no BOM"


def test_the_two_shipped_scripts_are_found() -> None:
    assert {p.name for p in _shipped_scripts()} >= {"install.ps1", "first_user_journey.ps1"}


def test_windows_job_parses_the_files_the_way_powershell_file_reads_them() -> None:
    workflow = (ROOT / ".github" / "workflows" / "platform-smoke.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("\n  installer:") :]
    step = job[job.index("Parse the PowerShell scripts") :].split("\n      - name:")[0]
    assert "ParseFile" in step  # default-encoding read, as `powershell -File` does
    assert "ParseInput" in step and "UTF8" in step  # and the UTF-8 text the one-liner runs


# --- 2. effective execution policy -------------------------------------------------------------


def test_execution_policy_decision_uses_the_effective_policy() -> None:
    func = _function("Test-ExecutionPolicy")
    assert re.search(r"\$policy = Get-ExecutionPolicy\s*$", func, re.M), func
    assert "-Scope CurrentUser" not in func.split("Set-ExecutionPolicy")[0]


def test_a_policy_overridden_by_a_more_specific_scope_is_not_fatal() -> None:
    func = _function("Test-ExecutionPolicy")
    catch = func[func.index("catch {") :]
    assert "overridden" in catch
    assert "Stop-Installer" in catch  # still stops for a real failure
    assert catch.index("overridden") < catch.index("Stop-Installer")


# --- 9. no `exit` that closes the user's window under irm | iex ----------------------------------


def test_only_one_exit_statement_remains_and_it_is_in_the_final_helper() -> None:
    exits = re.findall(r"^\s*exit\b.*$", CODE, re.M)
    assert len(exits) == 1, exits
    assert "exit" in _function("Complete-Installer")


def test_the_helper_exits_only_when_run_from_a_script_file() -> None:
    func = _function("Complete-Installer")
    assert "$PSCommandPath" in func
    assert "LASTEXITCODE" in func


def test_every_stop_goes_through_stop_installer_and_main_ends_with_the_helper() -> None:
    assert len(re.findall(r"Stop-Installer\s+\d", CODE)) >= 10
    tail = TEXT[TEXT.rindex("Main") :]
    assert "Complete-Installer" in tail


def test_stop_installer_throws_so_cleanup_and_the_caller_continue() -> None:
    assert "throw" in _function("Stop-Installer")


# --- 10. a project folder that cannot be created is an error ------------------------------------


def test_creating_the_project_folder_fails_clearly() -> None:
    new_item = next(ln for ln in CODE.splitlines() if "New-Item -ItemType Directory" in ln)
    assert "-ErrorAction Stop" in new_item
    assert "Could not create the project directory" in CODE
    assert "Set-Location $projectDir -ErrorAction Stop" in CODE


# --- 5. the name is cleaned before the existence check ------------------------------------------


def test_project_name_is_cleaned_before_the_existing_folder_check() -> None:
    start = CODE.index("$projectDir = Read-Answer")
    block = CODE[start : CODE.index("break\n", start)]
    assert block.index("-replace") < block.index("Test-Path $projectDir")
    assert CODE.count("$projectDir.ToLower() -replace") == 1  # no second clean-up afterwards


# --- 8. the session PATH is extended, not replaced --------------------------------------------


def test_session_path_is_appended_to() -> None:
    assignments = re.findall(r"^\s*\$env:Path\s*=.*$", CODE, re.M)
    assert assignments
    assert all("$env:Path" in a.split("=", 1)[1] for a in assignments), assignments


# --- 6. pip --user fails inside an active virtual environment ---------------------------------


def test_global_install_inside_a_virtual_environment_stops_with_advice() -> None:
    func = _function("Install-DangoGlobal")
    assert "base_prefix" in func
    assert "inside an active virtual environment" in func
    assert "deactivate" in func
    assert func.index("base_prefix") < func.index("pip install --user")
    guard = func[func.index('if ($inVenv -eq "1") {') :].split("\n    }\n")[0]
    assert "Stop-Installer 1" in guard  # the guard stops the run, it does not just warn


# --- 11, 12. clear next steps -----------------------------------------------------------------


def test_failed_init_prints_what_to_run_and_where_the_log_is() -> None:
    func = _function("Initialize-Project")
    after = func[func.index("Failed to initialize project") :]
    for text in ("dango init", ".dango\\logs\\dango.log", "Set-Location", "Stop-Installer 1"):
        assert text in after, text


def test_dango_not_on_path_prints_the_full_command_for_both_global_flows() -> None:
    assert "Dango is installed, but 'dango' is not on your PATH yet" in CODE
    assert "the project was not initialised" in CODE
    assert CODE.count("$script:DangoExe") >= 4  # set once, shown in both global flows


def test_venv_instructions_do_not_claim_the_environment_is_active() -> None:
    func = _function("Write-ActivationInstructions")
    assert "activated and ready" not in func
    assert "(Get-Location).Path" in func
    assert "Activate.ps1" in func


# --- the Windows installer job proves the stop behaviour; a Linux leg runs the installer ----------


def test_windows_job_runs_the_installer_stop_behaviour_both_ways() -> None:
    workflow = (ROOT / ".github" / "workflows" / "platform-smoke.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("\n  installer:") : workflow.index("\n  installer-containers:")]
    step = job[job.index("Installer stop behaviour") :].split("\n      - name:")[0]
    assert "windows-latest" in step
    assert "-File install.ps1" in step and "iex" in step and "session-still-open" in step


def test_linux_container_leg_runs_install_sh_in_ubuntu_22_04_and_debian_12() -> None:
    workflow = (ROOT / ".github" / "workflows" / "platform-smoke.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("\n  installer-containers:") :]
    assert "ubuntu:22.04" in job and "debian:12" in job
    assert "DANGO_INSTALL_SPEC" in job and "DANGO_INSTALL_NONINTERACTIVE=1" in job
    assert "/repo:ro" in job and "/dist:ro" in job  # read-only mounts of the checkout and wheel
    assert "docker run --rm" in job  # nothing is left behind
    assert "continue-on-error" not in job
