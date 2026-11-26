"""tests/unit/test_install_pip_and_source_wording.py

The installers' pip self-upgrade goes through `python -m pip` (a Windows pip cannot replace its own
pip.exe) and never shows an error, and the install messages say where Dango comes from when
DANGO_INSTALL_SPEC is set while the default transcript is unchanged. install.sh is run against
fake interpreters; install.ps1 is checked structurally (PowerShell is not run here).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.factories.installer_fakes import run_installer

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
PS1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
PS1_CODE = "\n".join(ln for ln in PS1.splitlines() if not ln.lstrip().startswith("#"))
ANSI = re.compile(r"\x1b\[[0-9;]*m")
WHEEL = "/work/dist/getdango-1.0.15-py3-none-any.whl"
WIN_WHEEL = "C:\\work\\new\\tab\\dist\\getdango-1.0.15-py3-none-any.whl"


def _ps1_function(name: str) -> str:
    start = PS1.index(f"function {name}")
    return PS1[start : PS1.index("\n}\n", start)]


# --- 13. the pip self-upgrade goes through `python -m pip` ---------------------------------------


def test_sh_upgrades_pip_through_the_interpreter(tmp_path: Path) -> None:
    run = run_installer(tmp_path, {}, snippet="install_dango venv", seed_project="venv")
    assert run.proc.returncode == 0, run.output
    assert ["-m", "pip", "install", "--upgrade", "pip", "-q"] in run.calls()


def test_ps1_never_upgrades_pip_through_pip_exe() -> None:
    for line in PS1_CODE.splitlines():
        if "--upgrade pip" in line:
            assert "-m pip install --upgrade pip" in line, line
    assert not re.search(r"&\s*pip\s+install\s+--upgrade\s+pip", PS1_CODE)


def test_ps1_pip_upgrade_uses_the_venv_python_and_is_not_fatal() -> None:
    func = _ps1_function("Install-Dango")
    upgrade = func[func.index("--upgrade pip") :]
    before = func[: func.index("--upgrade pip")]
    assert "python.exe" in before.splitlines()[-1] or "$venvPython" in before.splitlines()[-1]
    check = upgrade[: upgrade.index("pip install $script:DangoSpec")]
    assert "$LASTEXITCODE" in check  # the result is looked at ...
    assert "Write-Warning-Message" in check  # ... said, not raised ...
    assert "Stop-Installer" not in check  # ... and the install goes on


# --- 14. the install messages say where Dango comes from, only under the override ---------------


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ("install_dango venv", f"Installing Dango from {WHEEL}..."),
        ("upgrade_dango venv", f"Upgrading Dango from {WHEEL}..."),
        ("install_dango_global", f"Installing Dango globally from {WHEEL}..."),
    ],
    ids=["venv-install", "venv-upgrade", "global-install"],
)
def test_sh_step_names_the_spec_when_it_is_set(tmp_path: Path, snippet: str, message: str) -> None:
    run = run_installer(
        tmp_path, {"DANGO_INSTALL_SPEC": WHEEL}, snippet=snippet, seed_project="venv"
    )
    assert run.proc.returncode == 0, run.output
    assert message in run.proc.stdout


@pytest.mark.parametrize(
    ("snippet", "fail", "message"),
    [
        ("install_dango venv", "install", f"Failed to install Dango from {WHEEL}"),
        ("upgrade_dango venv", "dango", f"Failed to upgrade Dango from {WHEEL}"),
        ("install_dango_global", "install", f"Failed to install Dango from {WHEEL}"),
    ],
    ids=["venv-install", "venv-upgrade", "global-install"],
)
def test_sh_failure_names_the_spec_when_it_is_set(
    tmp_path: Path, snippet: str, fail: str, message: str
) -> None:
    env = {"DANGO_INSTALL_SPEC": WHEEL, "FAKE_PIP_FAIL": fail}
    run = run_installer(tmp_path, env, snippet=snippet, seed_project="venv")
    assert run.proc.returncode != 0
    assert message in run.proc.stdout


def test_sh_prints_a_spec_with_backslashes_as_typed(tmp_path: Path) -> None:
    run = run_installer(
        tmp_path,
        {"DANGO_INSTALL_SPEC": WIN_WHEEL},
        snippet="install_dango venv",
        seed_project="venv",
    )
    assert run.proc.returncode == 0, run.output
    assert f"Installing Dango from {WIN_WHEEL}..." in run.proc.stdout  # no \n, \t or \b mangling


@pytest.mark.parametrize(
    ("snippet", "fail", "step", "failure"),
    [
        ("install_dango venv", "install", "Installing Dango...", "Failed to install Dango"),
        ("upgrade_dango venv", "dango", "Upgrading Dango...", "Failed to upgrade Dango"),
        (
            "install_dango_global",
            "install",
            "Installing Dango globally...",
            "Failed to install Dango",
        ),
    ],
    ids=["venv-install", "venv-upgrade", "global-install"],
)
def test_sh_default_messages_do_not_change(
    tmp_path: Path, snippet: str, fail: str, step: str, failure: str
) -> None:
    (tmp_path / "ok").mkdir()
    (tmp_path / "bad").mkdir()
    ok = run_installer(tmp_path / "ok", {}, snippet=snippet, seed_project="venv")
    bad = run_installer(
        tmp_path / "bad", {"FAKE_PIP_FAIL": fail}, snippet=snippet, seed_project="venv"
    )
    for out, text in ((ok.proc.stdout, step), (bad.proc.stdout, failure)):
        lines = [ANSI.sub("", ln) for ln in out.splitlines()]
        assert any(ln.endswith(text) for ln in lines), (text, lines)


def test_ps1_source_text_is_set_once_from_the_override() -> None:
    func = _ps1_function("Test-InstallSpec")
    # the default texts are what they always were ...
    assert '$script:DangoSource = "PyPI"' in func
    assert '$script:DangoFrom = ""' in func
    # ... and only the override names the spec
    assert "$script:DangoSource = $spec" in func
    assert '$script:DangoFrom = " from $spec"' in func
    assert func.index('$script:DangoSource = "PyPI"') < func.index("$script:DangoSource = $spec")


@pytest.mark.parametrize(
    ("function", "text"),
    [
        ("Install-Dango", 'Write-Step "Installing Dango from $script:DangoSource..."'),
        ("Install-Dango", 'Write-Error-Message "Failed to install Dango$script:DangoFrom"'),
        ("Update-Dango", 'Write-Step "Upgrading Dango$script:DangoFrom..."'),
        ("Update-Dango", 'Write-Error-Message "Failed to upgrade Dango$script:DangoFrom"'),
        ("Install-DangoGlobal", 'Write-Step "Installing Dango globally$script:DangoFrom..."'),
        (
            "Install-DangoGlobal",
            'Write-Error-Message "Failed to install Dango from $script:DangoSource"',
        ),
    ],
)
def test_ps1_messages_name_the_source(function: str, text: str) -> None:
    assert text in _ps1_function(function)


def test_ps1_no_message_hardcodes_pypi_as_the_source() -> None:
    hardcoded = [ln for ln in PS1_CODE.splitlines() if "Write-" in ln and "from PyPI" in ln]
    assert not hardcoded, hardcoded
