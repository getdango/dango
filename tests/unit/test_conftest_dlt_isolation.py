"""tests/unit/test_conftest_dlt_isolation.py

The test suite never sees the developer's DLT_DATA_DIR or XDG_DATA_HOME: dlt prefers both over HOME,
so a test that removes a pipeline directory could otherwise delete real state.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROBE = "DANGO_CONFTEST_PROBE"


def test_probe() -> None:
    """Runs for real only in the child process below; a plain run has nothing to check."""
    if os.environ.get(PROBE) != "1":
        return
    assert "DLT_DATA_DIR" not in os.environ
    assert "XDG_DATA_HOME" not in os.environ


def test_the_suite_unsets_both_variables_the_developer_exported(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env.update(
        {
            PROBE: "1",
            "DLT_DATA_DIR": str(tmp_path / "real_dlt"),
            "XDG_DATA_HOME": str(tmp_path / "real_xdg"),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        }
    )
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"{Path(__file__).as_posix()}::test_probe",
            "-q",
            "-p",
            "no:randomly",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert done.returncode == 0, done.stdout[-1500:]
    assert not (tmp_path / "real_dlt").exists()
