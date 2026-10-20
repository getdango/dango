"""tests/unit/test_root_compose_scheduled.py

The root timer's Metabase stop/start/volume lookup in cloud/scheduled_backup.py: the compose
helper when the marker exists (busy/failure handling, order, bounded waits), the unchanged
legacy commands otherwise.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import _server_templates as templates
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import scheduled_backup as sb

pytestmark = pytest.mark.unit

_MODULE = "dango.platform.cloud.scheduled_backup"
_HELPER = ch.HELPER_PATH
#: One helper call may take at most this long inside the timer's 900 s unit budget.
_CALL_LIMIT = 120


class _Proc:
    def __init__(self, runner: _Runner, argv: list[str], done: Any) -> None:
        self.runner, self.argv, self.done = runner, argv, done
        self.returncode = done.returncode
        self.pid = 0

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.runner.communicate_timeouts.append(timeout)
        verb = " ".join(self.argv[1:])
        action = self.runner.raises.get(verb)
        if action == "hang":
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        if isinstance(action, type) and issubclass(action, BaseException):
            raise action(1)
        return self.done.stdout, self.done.stderr

    def terminate(self) -> None:
        self.runner.signals.append("terminate")

    def kill(self) -> None:
        self.runner.signals.append("kill")

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode


class _Runner:
    """Stands in for ``subprocess.run``/``Popen``; helper exit codes come from ``rc`` per verb."""

    def __init__(self, rc: dict[str, int | list[int]] | None = None, volume: str = "/vol") -> None:
        self.rc = {k: ([v] if isinstance(v, int) else list(v)) for k, v in (rc or {}).items()}
        self.volume = volume
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.communicate_timeouts: list[float | None] = []
        self.signals: list[str] = []
        self.raises: dict[str, Any] = {}

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        code, out = 0, ""
        if argv and argv[0] == _HELPER:
            verb = " ".join(argv[1:])
            codes = self.rc.get(verb) or [0]
            code = codes.pop(0) if len(codes) > 1 else codes[0]
            out = self.volume + "\n" if verb == "volume-path" and code == 0 else ""
        return subprocess.CompletedProcess(argv, code, out, "")

    def popen(self, argv: list[str], **kwargs: Any) -> _Proc:
        return _Proc(self, argv, self(argv, **kwargs))

    def events(self) -> list[str]:
        result = []
        for argv, _ in self.calls:
            if argv[0] == _HELPER:
                result.append("helper:" + " ".join(argv[1:]))
            elif argv[:2] == ["systemctl", "stop"]:
                result.append("web:stop")
            elif argv[:2] == ["systemctl", "start"]:
                result.append("web:start")
            else:
                result.append(" ".join(argv))
        return result


@contextlib.contextmanager
def _env(runner: _Runner, *, helper: bool) -> Iterator[None]:
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(f"{_MODULE}.subprocess.run", runner))
        stack.enter_context(patch(f"{_MODULE}.subprocess.Popen", runner.popen))
        stack.enter_context(
            patch(f"{_MODULE}.get_compose_project_name", return_value="dango-ab12cd34")
        )
        if helper:
            stack.enter_context(patch(f"{_MODULE}._helper_enabled", return_value=True, create=True))
        yield


class TestHelperStopStart:
    def test_stop_order_metabase_web_metabase(self) -> None:
        runner = _Runner()
        with _env(runner, helper=True):
            sb._stop_services()
        assert runner.events() == ["helper:stop metabase", "web:stop", "helper:stop metabase"]
        assert not any(argv[0] == "docker" for argv, _ in runner.calls)

    def test_busy_first_stop_aborts_with_nothing_stopped(self) -> None:
        runner = _Runner({"stop metabase": 75})
        with _env(runner, helper=True), pytest.raises(CloudProvisioningError, match="(?i)progress"):
            sb._stop_services()
        assert runner.events() == ["helper:stop metabase"]

    def test_busy_second_stop_restarts_dango_web_first(self) -> None:
        runner = _Runner({"stop metabase": [0, 75]})
        with _env(runner, helper=True), pytest.raises(CloudProvisioningError, match="(?i)progress"):
            sb._stop_services()
        events = runner.events()
        assert events.index("web:stop") < events.index("web:start")

    @pytest.mark.parametrize("code", [64, 127])
    def test_failed_stop_is_a_failure_and_restarts(self, code: int) -> None:
        runner = _Runner({"stop metabase": code})
        with _env(runner, helper=True), pytest.raises(CloudProvisioningError):
            sb._stop_services()
        assert "web:stop" not in runner.events()
        assert "web:start" in runner.events()

    def test_timed_out_first_stop_runs_the_restart_path(self) -> None:
        runner = _Runner()
        runner.raises["stop metabase"] = "hang"
        with _env(runner, helper=True), pytest.raises(CloudProvisioningError):
            sb._stop_services()
        events = runner.events()
        assert "web:start" in events
        assert events.index("web:start") < events.index("helper:start metabase")

    def test_start_runs_dango_web_before_the_helper(self) -> None:
        runner = _Runner({"start metabase": 75})
        with _env(runner, helper=True):
            sb._start_services()
        assert runner.events() == ["web:start", "helper:start metabase"]

    def test_interrupted_second_stop_still_starts_dango_web(self, tmp_path: Path) -> None:
        runner = _Runner()
        with _env(runner, helper=True):
            real = runner.popen
            seen = {"n": 0}

            def popen(argv: list[str], **kw: Any) -> _Proc:
                if argv[1:] == ["stop", "metabase"]:
                    seen["n"] += 1
                    if seen["n"] == 2:
                        raise SystemExit(1)
                return real(argv, **kw)

            with (
                patch(f"{_MODULE}.subprocess.Popen", popen),
                pytest.raises(SystemExit),
                patch(f"{_MODULE}.BACKUP_DIR", tmp_path),
            ):
                sb._create_local_archive("scheduled")
        assert "web:start" in runner.events()

    def test_each_helper_call_is_bounded(self) -> None:
        runner = _Runner()
        with _env(runner, helper=True):
            sb._stop_services()
            sb._start_services()
        assert runner.communicate_timeouts
        assert all(t is not None and t <= _CALL_LIMIT for t in runner.communicate_timeouts)

    def test_hung_call_is_terminated_then_killed(self) -> None:
        runner = _Runner()
        runner.raises["volume-path"] = "hang"
        with _env(runner, helper=True):
            assert sb._get_metabase_volume_path() is None
        assert runner.signals[0] == "terminate"

    def test_helper_is_called_directly_not_through_sudo(self) -> None:
        runner = _Runner()
        with _env(runner, helper=True):
            sb._stop_services()
        assert all(argv[0] != "sudo" for argv, _ in runner.calls)


class TestHelperArchive:
    def test_busy_stop_never_restarts_anything(self, tmp_path: Path) -> None:
        runner = _Runner({"stop metabase": 75})
        with (
            _env(runner, helper=True),
            patch(f"{_MODULE}.BACKUP_DIR", tmp_path),
            pytest.raises(CloudProvisioningError),
        ):
            sb._create_local_archive("scheduled")
        assert runner.events() == ["helper:stop metabase"]


class TestVolumePath:
    def test_uses_the_helper_verb(self) -> None:
        runner = _Runner(volume="/var/lib/docker/volumes/x/_data")
        with _env(runner, helper=True):
            assert sb._get_metabase_volume_path() == "/var/lib/docker/volumes/x/_data"
        assert [a for a, _ in runner.calls] == [[_HELPER, "volume-path"]]

    def test_failed_lookup_is_none(self) -> None:
        runner = _Runner({"volume-path": 1})
        with _env(runner, helper=True):
            assert sb._get_metabase_volume_path() is None


class TestLegacyUnchanged:
    def test_service_commands(self) -> None:
        runner = _Runner()
        with _env(runner, helper=False):
            sb._stop_services()
            sb._start_services()
        compose = "/srv/dango/project/docker-compose.yml"
        assert [a for a, _ in runner.calls] == [
            ["systemctl", "stop", "dango-web"],
            ["docker", "compose", "-f", compose, "stop", "metabase"],
            ["docker", "compose", "-f", compose, "start", "metabase"],
            ["systemctl", "start", "dango-web"],
        ]

    def test_volume_lookup(self) -> None:
        runner = _Runner()
        with _env(runner, helper=False):
            sb._get_metabase_volume_path()
        assert runner.calls[0][0] == [
            "docker",
            "volume",
            "inspect",
            "dango-ab12cd34_metabase-data",
            "--format",
            "{{.Mountpoint}}",
        ]


class TestHelperEnabledCheck:
    def test_requires_root_owned_non_writable_parents(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        srv, compose = tmp_path / "srv", tmp_path / "srv" / "compose"
        compose.mkdir(parents=True)
        monkeypatch.setattr(sb, "_HELPER_PARENT_DIRS", (str(srv), str(compose)))
        monkeypatch.setattr(sb, "_TRUSTED_UID", os.getuid())
        monkeypatch.setattr(sb, "uses_helper", lambda: True)
        srv.chmod(0o755)
        compose.chmod(0o755)
        assert sb._helper_enabled() is True
        compose.chmod(0o775)
        assert sb._helper_enabled() is False
        compose.chmod(0o755)
        monkeypatch.setattr(sb, "_TRUSTED_UID", os.getuid() + 1)
        assert sb._helper_enabled() is False

    def test_marker_check_failing_means_legacy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sb, "uses_helper", lambda: False)
        assert sb._helper_enabled() is False


class TestBackupUnit:
    def test_unit_leaves_time_for_the_restart_path(self) -> None:
        unit = templates.SYSTEMD_BACKUP_SERVICE
        stop = [line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")]
        assert stop and int(stop[0].split("=")[1].rstrip("s")) >= 300
