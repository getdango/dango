"""tests/unit/test_install_script_python_range.py

Installer interpreter selection: install.sh / install.ps1 accept Python 3.10-3.13 and reject anything else.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"


def _make_path(tmp_path: Path, stubs: dict[str, str]) -> str:
    """Build an isolated PATH holding `cut` plus stub interpreters printing the given version."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cut = shutil.which("cut")
    assert cut is not None
    (bindir / "cut").symlink_to(cut)
    for name, version in stubs.items():
        stub = bindir / name
        stub.write_text(f"#!/bin/bash\necho {version}\n")
        stub.chmod(0o755)
    return str(bindir)


def _run_check(tmp_path: Path, stubs: dict[str, str]) -> subprocess.CompletedProcess[str]:
    path = _make_path(tmp_path, stubs)
    script = f'DANGO_INSTALL_SOURCED=1 source "{INSTALL_SH}"; check_python; echo "CMD=$PYTHON_CMD VER=$PYTHON_VERSION"'
    return subprocess.run(
        [shutil.which("bash") or "/bin/bash", "-c", script],
        env={"PATH": path, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_install_sh_syntax_valid() -> None:
    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("version", ["3.10", "3.11", "3.12", "3.13"])
def test_supported_versions_accepted(tmp_path: Path, version: str) -> None:
    result = _run_check(tmp_path, {"python3": version})
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"CMD=python3 VER={version}" in result.stdout


@pytest.mark.parametrize("version", ["3.9", "3.14"])
def test_unsupported_versions_rejected(tmp_path: Path, version: str) -> None:
    result = _run_check(tmp_path, {"python3": version})
    assert result.returncode != 0
    assert "CMD=" not in result.stdout
    assert "3.10-3.13" in result.stdout


def test_python313_preferred_over_older(tmp_path: Path) -> None:
    result = _run_check(tmp_path, {"python3.13": "3.13", "python3.12": "3.12"})
    assert result.returncode == 0
    assert "CMD=python3.13 VER=3.13" in result.stdout


def test_no_stale_python_range_text() -> None:
    for path in (INSTALL_SH, INSTALL_PS1):
        text = path.read_text()
        assert "3.13+ not yet supported" not in text, path.name
        assert "3.10-3.12" not in text, path.name
        assert "Version: 0.1.0" not in text, path.name


def test_install_ps1_accepts_313() -> None:
    text = INSTALL_PS1.read_text()
    assert "$minor -le 13" in text
    assert '"python3.13"' in text
