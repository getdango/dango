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
    assert re.search(r"if \(Test-ScriptPolicyAllows \$now\) \{[^}]*return", override)
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
    decision = exit_line.group(1)
    assert "$PSCommandPath" in decision and "$oneShot" in decision
    # a successful run never calls exit in a host (the VS Code terminal starts with -Command and
    # `irm | iex; more` must still run `more`); only a failure in a console window does
    assert "$Code -ne 0" in decision and "$Host.Name -eq 'ConsoleHost'" in decision
    # -NoExit wins over -Command
    assert re.search(r"if \(\$keepOpen\) \{\n\s+\$oneShot = \$false", func), func


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


# --- the command-line scan ---------------------------------------------------------------------------


def _scan_patterns() -> dict[str, str]:
    func = _function("Complete-Installer")
    found = re.findall(r"\$arg -match '([^']+)'", func)
    names = ["command", "encoded", "noexit", "file"]
    assert len(found) == len(names), found
    return dict(zip(names, found, strict=True))


@pytest.mark.parametrize(
    ("kind", "arg", "expected"),
    [
        ("command", "-Command", True),
        ("command", "-c", True),
        ("command", "-co", True),
        ("command", "-COMMAND", True),
        ("command", "--command", True),
        ("command", "/c", True),
        ("command", "-cx", False),
        ("command", "-File", False),
        ("command", "command", False),
        ("encoded", "-e", True),
        ("encoded", "-ec", True),
        ("encoded", "-enc", True),
        ("encoded", "-EncodedCommand", True),
        ("encoded", "--encodedcommand", True),
        ("encoded", "-ex", False),
        ("noexit", "-noe", True),
        ("noexit", "-noex", True),
        ("noexit", "-NoExit", True),
        ("noexit", "--noexit", True),
        ("noexit", "-no", False),
        ("noexit", "-NoProfile", False),
        ("file", "-File", True),
        ("file", "-f", True),
        ("file", "--file", True),
        ("file", "-fx", False),
    ],
)
def test_the_scan_recognises_each_spelling_of_the_options(
    kind: str, arg: str, expected: bool
) -> None:
    assert bool(re.search(_scan_patterns()[kind], arg, re.I)) is expected


def test_the_scan_stops_at_the_first_command_or_file_option() -> None:
    func = _function("Complete-Installer")
    # tokens after -Command belong to the command text, so a later -NoExit must not count
    assert re.search(r"\$oneShot = \$true\s+break", func), func
    assert "break" in func[func.index("-match '") :]


# --- policy resolution ----------------------------------------------------------------------------------


def test_the_set_of_policies_that_allow_scripts_is_exactly_three() -> None:
    func = _function("Test-ScriptPolicyAllows")
    words = set(re.findall(r'"(\w+)"', func))
    assert words == {"RemoteSigned", "Unrestricted", "Bypass"}  # AllSigned blocks Activate.ps1


def test_the_process_note_resolves_the_saved_policy_through_group_policy_first() -> None:
    func = _function("Test-ExecutionPolicy")
    assert "'MachinePolicy','UserPolicy','CurrentUser','LocalMachine'" in func
    assert re.search(r"if \(-not \(Test-ScriptPolicyAllows \$saved\)\) \{", func)
    assert re.search(
        r"if \(Test-ScriptPolicyAllows \(Get-ExecutionPolicy -Scope Process\)\) \{", func
    )


def test_the_blocked_message_gives_only_advice_that_can_help() -> None:
    func = _function("Test-ExecutionPolicy")
    blocked = func[func.index("is blocked by a policy") :]
    assert "Get-ExecutionPolicy -Scope MachinePolicy" in blocked
    assert "Get-ExecutionPolicy -Scope UserPolicy" in blocked
    assert "Group Policy" in blocked
    assert "was saved" in func  # the CurrentUser write is stated outright


# --- the Windows job covers a successful one-shot run too ------------------------------------------------


def test_windows_job_checks_that_a_successful_one_shot_run_goes_on() -> None:
    job = WORKFLOW[WORKFLOW.index("\n  installer:") : WORKFLOW.index("\n  installer-containers:")]
    step = job[job.index("Installer stop behaviour") :].split("\n      - name:")[0]
    assert "after-installer" in step
    assert "DANGO_INSTALL_EXISTING_ACTION" in step  # cancel path: exit code 0, no install
