"""tests/unit/test_server_python_ensure.py

ensure_server_python and the version gates, against a scripted fake server.
"""

from __future__ import annotations

import importlib
from types import ModuleType

import pytest

from dango.platform.cloud.ssh import CommandResult

_HOST_MARK = 'command -v "$n"'
_INSTALL_MARK = "curl --proto"
_CHECK_MARK = ".dango-python.sha256"


def _sps() -> ModuleType:
    return importlib.import_module("dango.platform.cloud.server_python_scripts")


def _sp() -> ModuleType:
    return importlib.import_module("dango.platform.cloud.server_python")


# ---------------------------------------------------------------------------
# ensure_server_python with a scripted fake SSH
# ---------------------------------------------------------------------------


class _FakeServer:
    def __init__(
        self,
        *,
        host: str = "",
        arch: str = "x86_64",
        installed: bool = False,
        install_code: int = 0,
        probe: str = "3.12.16.final",
    ) -> None:
        self.host, self.arch, self.installed = host, arch, installed
        self.install_code, self.probe = install_code, probe
        self.commands: list[str] = []
        self.install_timeouts: list[int | None] = []

    def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
        self.commands.append(command)
        if _INSTALL_MARK in command:
            self.install_timeouts.append(timeout)
        if _HOST_MARK in command:
            return CommandResult(
                stdout=self.host + "\n" if self.host else "", stderr="", exit_code=0
            )
        if command == "uname -m":
            return CommandResult(stdout=self.arch + "\n", stderr="", exit_code=0)
        if _INSTALL_MARK in command:
            if self.install_code == 0:
                self.installed = True
                return CommandResult(stdout="", stderr="", exit_code=0)
            return CommandResult(
                stdout="", stderr="curl: (6) Could not resolve host", exit_code=self.install_code
            )
        if _CHECK_MARK in command:
            if self.installed:
                return CommandResult(stdout=self.probe + "\n", stderr="", exit_code=0)
            return CommandResult(stdout="", stderr="", exit_code=1)
        return CommandResult(stdout="", stderr="", exit_code=0)

    def count(self, mark: str) -> int:
        return sum(mark in c for c in self.commands)


@pytest.mark.unit
class TestEnsure:
    def test_host_interpreter_is_used_and_nothing_is_downloaded(self) -> None:
        server = _FakeServer(host="/usr/bin/python3.12")
        warnings: list[str] = []
        assert _sp().ensure_server_python(server, warnings=warnings) == "/usr/bin/python3.12"
        assert server.count(_INSTALL_MARK) == 0 and server.count("uname") == 0
        assert warnings == []

    @pytest.mark.parametrize(
        "bad", ["python3", "/usr/bin/py thon", "/usr/bin/x;rm", "/usr/../tmp/p"]
    )
    def test_unexpected_host_output_is_not_trusted(self, bad: str) -> None:
        server = _FakeServer(host=bad, installed=True)
        assert _sp().ensure_server_python(server, warnings=[]) == _sps().PYTHON_BIN

    @pytest.mark.parametrize("machine", ["riscv64", "armv7l", "s390x", ""])
    def test_unsupported_architecture_is_refused_before_any_download(self, machine: str) -> None:
        sp = _sp()
        server = _FakeServer(arch=machine)
        with pytest.raises(sp.ServerPythonError) as excinfo:
            sp.ensure_server_python(server, warnings=[])
        message = str(excinfo.value)
        assert "Python 3.11, 3.12 or 3.13" in message and "architecture" in message
        assert excinfo.value.error_code == "DANGO-D024"
        assert server.count(_INSTALL_MARK) == 0

    def test_installed_pinned_interpreter_is_reused_without_a_download(self) -> None:
        server = _FakeServer(installed=True)
        assert _sp().ensure_server_python(server, warnings=[]) == _sps().PYTHON_BIN
        assert server.count(_INSTALL_MARK) == 0

    def test_install_then_verify_then_idempotent(self) -> None:
        server = _FakeServer()
        warnings: list[str] = []
        assert _sp().ensure_server_python(server, warnings=warnings) == _sps().PYTHON_BIN
        assert server.count(_INSTALL_MARK) == 1
        assert server.count(_CHECK_MARK) == 3  # before, the install itself (marker), after
        assert any("Installed Python 3.12.16" in w for w in warnings)
        (warning,) = warnings
        assert "no usable root-owned Python 3.11, 3.12 or 3.13" in warning
        assert "apt install" not in warning  # 22.04's own venv package would not help
        server.commands.clear()
        assert _sp().ensure_server_python(server, warnings=[]) == _sps().PYTHON_BIN
        assert server.count(_INSTALL_MARK) == 0  # second run installs nothing

    def test_in_range_python_without_venv_is_named_in_the_warning(self) -> None:
        server = _FakeServer(host="novenv /usr/bin/python3.12")
        warnings: list[str] = []
        assert _sp().ensure_server_python(server, warnings=warnings) == _sps().PYTHON_BIN
        assert server.count(_INSTALL_MARK) == 1  # the host interpreter is not used
        (warning,) = warnings
        for phrase in ("/usr/bin/python3.12", "venv", "ensurepip", "apt install python3-venv"):
            assert phrase in warning, phrase

    def test_install_uses_the_long_ssh_timeout(self) -> None:
        sp = _sp()
        server = _FakeServer()
        sp.ensure_server_python(server, warnings=[])
        assert server.install_timeouts == [sp.INSTALL_STEP_TIMEOUT]

    def test_wrong_installed_version_is_reinstalled(self) -> None:
        server = _FakeServer(installed=True, probe="3.12.15.final")
        sp = _sp()
        with pytest.raises(sp.ServerPythonError, match="failed its check"):
            sp.ensure_server_python(server, warnings=[])
        assert server.count(_INSTALL_MARK) == 1

    @pytest.mark.parametrize(
        ("code", "phrase"),
        [
            (10, "github.com"),
            (11, "wrong size"),
            (12, "SHA-256"),
            (13, "safety checks"),
            (14, "/opt/dango"),
            (15, "tool is missing"),
        ],
    )
    def test_install_failure_names_python_and_the_cause(self, code: int, phrase: str) -> None:
        sp = _sp()
        server = _FakeServer(install_code=code)
        with pytest.raises(sp.ServerPythonError) as excinfo:
            sp.ensure_server_python(server, warnings=[])
        message = str(excinfo.value)
        assert phrase in message and "Python 3.11, 3.12 or 3.13" in message
        assert "github.com and release-assets.githubusercontent.com" in message
        assert server.count(_CHECK_MARK) == 2  # no verification of a failed install

    def test_interrupted_run_converges_on_the_next_run(self) -> None:
        sp = _sp()
        server = _FakeServer(install_code=10)
        with pytest.raises(sp.ServerPythonError):
            sp.ensure_server_python(server, warnings=[])
        server.install_code = 0
        assert sp.ensure_server_python(server, warnings=[]) == _sps().PYTHON_BIN


@pytest.mark.unit
class TestVersionGates:
    @pytest.mark.parametrize(
        ("version", "needed"),
        [
            ("1.0.13", False),
            ("1.0.9", False),
            ("1.0.14", True),
            ("1.0.14rc1", True),
            ("1.0.15", True),
            ("2.0.0", True),
        ],
    )
    def test_needs_new_python(self, version: str, needed: bool) -> None:
        assert _sp().needs_new_python(version) is needed

    @pytest.mark.parametrize(
        ("recorded", "ok"),
        [
            ("3.10", False),
            ("3.11", True),
            ("3.12", True),
            ("3.13", True),
            ("3.14", False),
            ("", False),
        ],
    )
    def test_venv_python_is_read_as_data(self, recorded: str, ok: bool) -> None:
        commands: list[str] = []

        class _Cfg:
            def exec_command(self, command: str, timeout: int | None = None) -> CommandResult:
                commands.append(command)
                code = 0 if recorded else 1
                return CommandResult(stdout=recorded + "\n", stderr="", exit_code=code)

        assert _sp().venv_python_in_range(_Cfg()) is ok
        command = commands[0]
        assert "pyvenv.cfg" in command and "test ! -L" in command
        assert "/srv/dango/venv/bin/python" not in command  # never runs the venv
