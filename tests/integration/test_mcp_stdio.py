"""tests/integration/test_mcp_stdio.py

Spawns the real `dango mcp run` stdio server and checks the JSON-RPC stream stays clean.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESPONSE_TIMEOUT = 30.0
_PROTOCOL_VERSION = "2025-03-26"
_EXPECTED_CORE_TOOLS = {
    "list_sources",
    "create_source",
    "run_sync",
    "run_transform",
    "query",
    "create_model",
    "list_schedules",
    "run_doctor",
    "remote_status",
}
_CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("list_source_types", {}),
    ("list_sources", {}),
    ("get_platform_status", {}),
    ("query", {"sql": "SELECT 1"}),
)


def _env(project_root: Path) -> dict[str, str]:
    """Subprocess env: import the code under test, never the user's real project."""
    env = {k: v for k, v in os.environ.items() if k != "DANGO_PROJECT_ROOT"}
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{existing}" if existing else str(_REPO_ROOT)
    env["DANGO_PROJECT_ROOT"] = str(project_root)
    env["DANGO_LOG_LEVEL"] = "ERROR"
    # Unbuffered so a stray print() reaches the pipe immediately instead of sitting in a
    # block buffer until exit (which would hide it from a kill-at-teardown test).
    env["PYTHONUNBUFFERED"] = "1"
    return env


@dataclass
class Session:
    """A live `dango mcp run` process plus its collected stdout lines."""

    proc: subprocess.Popen[str]
    lines: queue.Queue[str] = field(default_factory=queue.Queue)
    raw_stdout: list[str] = field(default_factory=list)
    initialize_response: dict[str, Any] = field(default_factory=dict)
    tools_response: dict[str, Any] = field(default_factory=dict)
    tool_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    _next_id: int = 0

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return the response with the matching id."""
        self._next_id += 1
        req_id = self._next_id
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            try:
                line = self.lines.get(timeout=_RESPONSE_TIMEOUT)
            except queue.Empty:
                raise AssertionError(
                    f"no response to {method} within {_RESPONSE_TIMEOUT}s"
                ) from None
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # reported by test_stdout_is_only_jsonrpc
            if isinstance(msg, dict) and msg.get("id") == req_id:
                return msg

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})


def _drain(stream: Any) -> None:
    for _ in stream:
        pass


def _pump(stream: Any, sink: queue.Queue[str], raw: list[str]) -> None:
    for line in stream:
        raw.append(line)
        if line.strip():
            sink.put(line)


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Scaffold a blank scratch project under tmp (init starts no servers)."""
    base = tmp_path_factory.mktemp("mcp_stdio")
    proj = base / "proj"
    result = subprocess.run(
        [sys.executable, "-m", "dango.cli.main", "init", str(proj), "--skip-wizard"],
        env=_env(proj),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=base,
    )
    assert result.returncode == 0, f"init failed: {result.stdout}\n{result.stderr}"
    return proj


@pytest.fixture(scope="module")
def session(project: Path) -> Generator[Session, None, None]:
    """One real stdio server, initialized, shared by the read-only tests."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "dango.cli.main", "mcp", "run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(project),
        cwd=project,
    )
    sess = Session(proc=proc)
    assert proc.stdout is not None and proc.stderr is not None
    threading.Thread(
        target=_pump, args=(proc.stdout, sess.lines, sess.raw_stdout), daemon=True
    ).start()
    threading.Thread(target=_drain, args=(proc.stderr,), daemon=True).start()
    try:
        sess.initialize_response = sess.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "dango-stdio-test", "version": "0"},
            },
        )
        sess.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        sess.tools_response = sess.request("tools/list")
        for name, args in _CALLS:
            sess.tool_responses[name] = sess.call_tool(name, args)
        yield sess
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_stdout_is_only_jsonrpc(session: Session) -> None:
    bad: list[str] = []
    for line in list(session.raw_stdout):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            bad.append(line)
            continue
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            bad.append(line)
    assert session.raw_stdout, "server produced no stdout at all"
    assert not bad, f"non-JSON-RPC stdout lines: {bad!r}"


def test_initialize_returns_instructions(session: Session) -> None:
    result = session.initialize_response.get("result")
    assert result is not None, session.initialize_response
    instructions = result.get("instructions")
    assert isinstance(instructions, str) and instructions
    assert "credentials_required" in instructions
    assert ".env" in instructions
    assert "local_files" in instructions
    assert len(instructions) <= 2100


def test_tools_list_contains_expected_tools(session: Session) -> None:
    result = session.tools_response.get("result")
    assert result is not None, session.tools_response
    names = {tool["name"] for tool in result["tools"]}
    assert _EXPECTED_CORE_TOOLS <= names, _EXPECTED_CORE_TOOLS - names
    assert "add_source" not in names


def test_tool_calls_return_results_not_protocol_errors(session: Session) -> None:
    assert set(session.tool_responses) == {name for name, _ in _CALLS}
    for name, response in session.tool_responses.items():
        assert "error" not in response, f"{name} returned a JSON-RPC error: {response}"
        assert "result" in response, f"{name} returned no result: {response}"
        # FastMCP reports a raising tool as a result with isError, not a JSON-RPC error.
        assert response["result"].get("isError") is not True, f"{name} raised: {response}"


def test_server_exits_when_stdin_closes(project: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "dango.cli.main", "mcp", "run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        env=_env(project),
        cwd=project,
    )
    try:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
