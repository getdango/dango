"""tests/unit/test_first_user_journey_ps1.py

scripts/first_user_journey.ps1 -Installer, checked structurally on any OS (PowerShell is not run
here): the installer must not inherit a process-level execution policy, and the user's previous
CurrentUser execution policy is restored when the run changed it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

TEXT = (Path(__file__).resolve().parents[2] / "scripts" / "first_user_journey.ps1").read_text(
    encoding="utf-8"
)


def _installer_branch() -> str:
    start = TEXT.index("if ($Installer) {")
    return TEXT[start : TEXT.index("} else {", start)]


def test_installer_branch_drops_the_inherited_process_execution_policy() -> None:
    branch = _installer_branch()
    assert "PSExecutionPolicyPreference" in branch
    assert branch.index("PSExecutionPolicyPreference") < branch.index("--installer")


def test_current_user_policy_is_recorded_before_the_installer_runs() -> None:
    record = TEXT.index("Get-ExecutionPolicy -Scope CurrentUser")
    assert record < TEXT.index("if ($Installer) {")
    assert re.search(r"\$previousPolicy\s*=\s*.*Get-ExecutionPolicy -Scope CurrentUser", TEXT)


def test_previous_policy_is_restored_in_a_finally_block_when_it_changed() -> None:
    assert "finally {" in TEXT
    final = TEXT[TEXT.index("finally {") :]
    assert "Set-ExecutionPolicy" in final and "$previousPolicy" in final
    assert re.search(r"-ne\s+\$previousPolicy", final), "restore only when the policy changed"


def test_the_policy_change_is_logged() -> None:
    final = TEXT[TEXT.index("finally {") :]
    assert re.search(r"Log .*execution policy", final, re.IGNORECASE)


def test_the_restore_sets_the_current_user_scope_only() -> None:
    final = TEXT[TEXT.index("finally {") :]
    call = next(ln for ln in final.splitlines() if "Set-ExecutionPolicy" in ln)
    assert "-Scope CurrentUser" in call
    assert "LocalMachine" not in final and "Process" not in call
