"""tests/unit/test_project_files_script.py

Run the generated root-side write script in a real shell against a temp tree.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from dango.platform.cloud.project_files import _WRITE_SCRIPT


def _gnu_mv() -> bool:
    mv = shutil.which("gmv") or shutil.which("mv")
    if not mv:
        return False
    out = subprocess.run([mv, "--version"], capture_output=True, text=True, check=False)
    return out.returncode == 0 and "GNU" in out.stdout


def _docker() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


def _run(tmp: Path, dest: Path, content: str) -> subprocess.CompletedProcess[str]:
    """Run the write script on the host (GNU mv) or in a python:3.11-slim container."""
    args = ["sh", "-c", _WRITE_SCRIPT, "_", str(dest), "600"]
    if _gnu_mv():
        return subprocess.run(args, input=content, capture_output=True, text=True, check=False)
    if _docker():
        return subprocess.run(
            ["docker", "run", "--rm", "-i", "-v", f"{tmp}:{tmp}", "python:3.11-slim", *args],
            input=content,
            capture_output=True,
            text=True,
            check=False,
        )
    pytest.skip("needs GNU mv or docker (python:3.11-slim)")


@pytest.mark.unit
class TestWriteScriptInRealShell:
    def test_replaces_regular_file(self, tmp_path):
        dest = tmp_path / "p" / ".env"
        dest.parent.mkdir()
        dest.write_text("old")
        result = _run(tmp_path, dest, "new")
        assert result.returncode == 0, result.stderr
        assert dest.read_text() == "new"
        assert [p.name for p in dest.parent.iterdir()] == [".env"]

    def test_refuses_directory_destination(self, tmp_path):
        dest = tmp_path / "p" / ".env"
        dest.mkdir(parents=True)
        result = _run(tmp_path, dest, "secret")
        assert result.returncode != 0
        assert list(dest.iterdir()) == []
        assert [p.name for p in dest.parent.iterdir()] == [".env"]

    def test_replaces_symlink_without_touching_target(self, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("keep")
        dest = tmp_path / "p" / ".env"
        dest.parent.mkdir()
        dest.symlink_to(target)
        result = _run(tmp_path, dest, "new")
        assert result.returncode == 0, result.stderr
        assert not dest.is_symlink()
        assert dest.read_text() == "new"
        assert target.read_text() == "keep"

    def test_symlink_to_directory_is_replaced_not_entered(self, tmp_path):
        target_dir = tmp_path / "elsewhere"
        target_dir.mkdir()
        dest = tmp_path / "p" / ".env"
        dest.parent.mkdir()
        dest.symlink_to(target_dir)
        result = _run(tmp_path, dest, "new")
        assert result.returncode == 0, result.stderr
        assert list(target_dir.iterdir()) == []
        assert not dest.is_symlink()
        assert dest.read_text() == "new"
