"""tests/unit/test_check_wheel_imports.py

scripts/check_wheel_imports.py imports every module of an installed package and reports failures.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_wheel_imports.py"


def _run(package: str, extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if extra_path is not None:
        env = {**os.environ, "PYTHONPATH": str(extra_path)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--package", package],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_passes_for_importable_package(tmp_path: Path) -> None:
    pkg = tmp_path / "goodpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "sub.py").write_text("import json\n")
    result = _run("goodpkg", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failures" in result.stdout


def test_fails_and_names_module_that_cannot_import(tmp_path: Path) -> None:
    pkg = tmp_path / "badpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "broken.py").write_text("import module_that_does_not_exist_xyz\n")
    result = _run("badpkg", tmp_path)
    assert result.returncode == 1
    assert "badpkg.broken" in result.stdout
    assert "module_that_does_not_exist_xyz" in result.stdout
