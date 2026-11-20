"""tests/unit/test_install_ps1_hooks.py

install.ps1 test hooks, checked structurally on any OS (PowerShell is not run here; the Windows
platform-smoke job runs the real script): the spec override at every pip install, and every
Read-Host prompt routed through the non-interactive Read-Answer helper.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "install.ps1").read_text(encoding="utf-8")
LINES = TEXT.splitlines()

# The nine Read-Host prompts of the original script, by answer variable (Docker is asked twice).
EXPECTED_SITES = [
    "DANGO_INSTALL_ALLOW_EXECUTION_POLICY",
    "DANGO_INSTALL_DOCKER_CONTINUE",
    "DANGO_INSTALL_DOCKER_CONTINUE",
    "DANGO_INSTALL_MODE",
    "DANGO_INSTALL_SETUP",
    "DANGO_INSTALL_ADD_TO_PATH",
    "DANGO_INSTALL_PROJECT_NAME",
    "DANGO_INSTALL_EXISTING_ACTION",
    "DANGO_INSTALL_CREATE_VENV",
]
HELPER = re.compile(r"^function Read-Answer \{\n.*?^\}\n", re.S | re.M)


def _outside_helper() -> str:
    assert HELPER.search(TEXT), "install.ps1 must define a Read-Answer helper"
    return HELPER.sub("", TEXT)


def test_no_prompt_uses_read_host_outside_the_helper() -> None:
    assert "Read-Host" not in _outside_helper()


def test_every_original_prompt_is_routed_through_read_answer() -> None:
    names = re.findall(r"Read-Answer\s+-Name\s+'(DANGO_INSTALL_\w+)'", _outside_helper())
    assert sorted(names) == sorted(EXPECTED_SITES)


def test_every_answer_variable_is_documented_in_the_header() -> None:
    header = "\n".join(LINES[:70])
    names = set(re.findall(r"Read-Answer\s+-Name\s+'(DANGO_INSTALL_\w+)'", TEXT))
    for name in names | {"DANGO_INSTALL_SPEC", "DANGO_INSTALL_NONINTERACTIVE"}:
        assert name in header, name


def test_the_helper_reads_the_noninteractive_switch_and_fails_naming_the_variable() -> None:
    body = HELPER.search(TEXT)
    assert body is not None
    text = body.group(0)
    assert "DANGO_INSTALL_NONINTERACTIVE" in text
    assert "exit 1" in text
    assert "Read-Host" in text


def test_every_dango_pip_install_uses_the_spec() -> None:
    sites = [
        ln
        for ln in LINES
        if "pip install" in ln
        and not ln.lstrip().startswith(("Write-Host", "#"))
        and "--upgrade pip" not in ln
    ]
    assert len(sites) == 3, sites
    for line in sites:
        assert "$script:DangoSpec" in line, line
        assert not re.search(r"\bgetdango\b", line), line


def test_spec_is_validated_before_anything_runs() -> None:
    assert "function Test-InstallSpec" in TEXT
    main = TEXT[TEXT.index("function Main {") :]
    assert main.index("Test-InstallSpec") < main.index("Test-ExecutionPolicy")


def test_noninteractive_project_init_skips_the_wizard() -> None:
    init = TEXT[TEXT.index("function Initialize-Project") :].split("\n}\n")[0]
    assert "--skip-wizard" in init
    assert "DANGO_INSTALL_NONINTERACTIVE" in init
