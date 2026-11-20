"""tests/unit/test_install_ps1_review.py

install.ps1 review fixes, checked structurally on any OS (the Windows platform-smoke jobs run the
real script): the execution-policy override branch, a Process-only policy, the exit code of a
one-shot `powershell -Command`, and guards for the Install-DangoGlobal return value and the stop
mechanism.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "install.ps1").read_text(encoding="utf-8")
CODE = "\n".join(ln for ln in TEXT.splitlines() if not ln.lstrip().startswith("#"))
WORKFLOW = (ROOT / ".github" / "workflows" / "platform-smoke.yml").read_text(encoding="utf-8")


def _function(name: str) -> str:
    start = TEXT.index(f"function {name}")
    return TEXT[start : TEXT.index("\n}\n", start)]


# --- an overridden policy continues only when scripts can now run --------------------------------


def test_override_is_matched_by_error_id_not_by_english_text() -> None:
    func = _function("Test-ExecutionPolicy")
    assert "FullyQualifiedErrorId" in func and "ExecutionPolicyOverride" in func
    assert "-match 'overridden'" not in func


def test_override_continues_only_after_rechecking_the_effective_policy() -> None:
    func = _function("Test-ExecutionPolicy")
    catch = func[func.index("catch {") :]
    override = catch[catch.index("ExecutionPolicyOverride") :]
    assert override.index("Get-ExecutionPolicy") < override.index("return")
    assert "Stop-Installer 1" in override  # still blocked: stop with a message of its own
    assert "blocked" in override


# --- a policy that allows scripts only for this process ---------------------------------------------


def test_process_only_policy_prints_a_note_about_new_windows() -> None:
    func = _function("Test-ExecutionPolicy")
    assert "Get-ExecutionPolicy -Scope Process" in func
    assert "new PowerShell window" in func and "Activate.ps1" in func


# --- the exit code of a one-shot `powershell -Command "irm | iex"` ----------------------------------


def test_one_shot_command_hosts_exit_with_the_code_but_interactive_windows_do_not() -> None:
    func = _function("Complete-Installer")
    assert "GetCommandLineArgs" in func
    assert "NoExit" in func or "noe" in func  # -NoExit keeps the window, so it must not exit
    exit_line = re.search(r"if \(([^\n]*)\) \{\n\s+exit \$Code", func)
    assert exit_line, func
    assert "$PSCommandPath" in exit_line.group(1)


def test_windows_job_checks_both_the_one_shot_and_the_interactive_form() -> None:
    job = WORKFLOW[WORKFLOW.index("\n  installer:") : WORKFLOW.index("\n  installer-containers:")]
    step = job[job.index("Installer stop behaviour") :].split("\n      - name:")[0]
    assert "-NoExit" in step  # interactive form: the session stays open
    assert "-Command" in step and "one-shot" in step.lower()  # one-shot form: exit code 1


# --- guards for behaviours a typo would silently break ----------------------------------------------


def test_pip_output_is_kept_out_of_the_return_value_of_install_dangoglobal() -> None:
    func = _function("Install-DangoGlobal")
    assert re.search(r"pip install --user \$script:DangoSpec \| Out-Host", func)


def test_the_boolean_result_is_compared_not_tested_for_truthiness() -> None:
    assert "$onPath -ne $true" in CODE
    assert "$success -eq $true" in CODE


def test_the_exit_decision_is_made_in_one_place_and_exits_with_the_code() -> None:
    func = _function("Complete-Installer")
    assert re.search(r"if \(\$PSCommandPath[^\n]*\) \{\n\s+exit \$Code\n\s+\}", func), func


def test_the_stop_exception_is_caught_by_type_before_the_generic_catch() -> None:
    tail = CODE[CODE.rindex("try {\n    Main") :]
    typed = tail.index("catch [System.OperationCanceledException]")
    generic = tail.index("catch {")
    assert typed < generic
