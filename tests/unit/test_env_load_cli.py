"""tests/unit/test_env_load_cli.py

A project `.env` that is not UTF-8 (cp1252 from Notepad) stops `sync`, `validate`, `doctor`, the
`oauth` commands and the MCP sync/doctor tools with one message that names the file: no
traceback, no raw codec text. A valid UTF-8 `.env` still syncs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.factories.scaffold_project import build_fresh_project, working_copy

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CP1252_ENV = b"caf\xe9=1\n"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
RAW = ("Traceback", "codec", "0xe9", "UnicodeDecodeError", "invalid continuation")


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path) -> Path:
    (tmp_path / "home").mkdir()
    root = working_copy(fresh_project, tmp_path)
    (root / ".env").write_bytes(CP1252_ENV)
    return root


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")


def _dango(root: Path, *args: str) -> tuple[int, str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "COLUMNS": "400",
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        stdin=subprocess.DEVNULL,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _assert_friendly(output: str) -> None:
    flat = " ".join(_ANSI.sub("", output).split())
    assert "Cannot read" in flat, output
    assert ".env" in flat, output
    assert "UTF-8" in flat, output
    for raw in RAW:
        assert raw not in output, f"{raw!r} leaked:\n{output}"


@pytest.mark.parametrize(
    "argv",
    [
        ["sync", "--yes"],
        ["validate"],
        ["doctor"],
        ["oauth", "check"],
        ["oauth", "setup", "google"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_non_utf8_env_stops_the_command_with_one_message(project: Path, argv: list[str]) -> None:
    code, output = _dango(project, *argv)
    assert code == 1, output
    _assert_friendly(output)


def test_sync_does_not_load_any_data_when_env_is_unreadable(project: Path) -> None:
    _dango(project, "sync", "--yes")
    assert not (project / "data" / "warehouse.duckdb").exists() or (
        (project / "data" / "warehouse.duckdb").stat().st_size < 1_000_000
    )
    assert not (project / "dbt" / "models" / "staging" / "stg_demo__demo.sql").exists()


def test_valid_utf8_env_still_syncs(project: Path) -> None:
    (project / ".env").write_bytes("café=1\nWP37_OK=yes\n".encode())
    code, output = _dango(project, "sync", "--yes")
    assert code == 0, output
    assert "Cannot read" not in output
    assert "Synced demo: 2 rows" in output


@pytest.fixture
def mcp_project(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DANGO_PROJECT_ROOT", str(project))
    monkeypatch.chdir(project)
    return project


def test_mcp_run_sync_returns_the_friendly_text(mcp_project: Path) -> None:
    from dango.cli.commands import mcp_operations

    result = mcp_operations.run_sync("demo")
    assert result["status"] == "failed", result
    text = json.dumps(result)
    assert "Cannot read" in text and "UTF-8" in text and ".env" in text, text
    for raw in ("codec", "0xe9", "invalid continuation"):
        assert raw not in text, text


def test_mcp_run_doctor_returns_the_friendly_text(mcp_project: Path) -> None:
    from dango.cli.commands import mcp_operations

    result = mcp_operations.run_doctor()
    text = json.dumps(result)
    assert "Cannot read" in text and "UTF-8" in text and ".env" in text, text
    for raw in ("codec", "0xe9", "invalid continuation"):
        assert raw not in text, text
