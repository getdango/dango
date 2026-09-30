"""tests/unit/test_process_manager_identity.py

Tests for is_project_server_running() — pid-file identity, never port-based.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dango.cli.helpers.process_manager import is_project_server_running, write_pid_file


@pytest.mark.unit
class TestIsProjectServerRunning:
    def test_is_project_server_running_false_without_pid_file(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        assert is_project_server_running(tmp_path) is False

    def test_is_project_server_running_true_for_live_pid(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        write_pid_file(tmp_path, os.getpid())
        assert is_project_server_running(tmp_path) is True

    def test_is_project_server_running_false_for_dead_pid(self, tmp_path: Path) -> None:
        (tmp_path / ".dango").mkdir()
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = proc.pid
        write_pid_file(tmp_path, pid)
        proc.wait()
        assert is_project_server_running(tmp_path) is False
