"""tests/unit/test_root_compose_scheduled.py

The root timer's Metabase stop/start/volume lookup in cloud/scheduled_backup.py: the compose
helper when the marker exists (busy handling, order, timeouts), the unchanged legacy commands otherwise.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import scheduled_backup as sb

pytestmark = pytest.mark.unit

_MODULE = "dango.platform.cloud.scheduled_backup"
_HELPER = ch.HELPER_PATH


class _Runner:
    """Stands in for ``subprocess.run``; helper exit codes come from ``rc`` per verb string."""

    def __init__(self, rc: dict[str, int | list[int]] | None = None, volume: str = "/vol") -> None:
        self.rc = {k: ([v] if isinstance(v, int) else list(v)) for k, v in (rc or {}).items()}
        self.volume = volume
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        code = 0
        out = ""
        if argv and argv[0] == _HELPER:
            verb = " ".join(argv[1:])
            codes = self.rc.get(verb) or [0]
            code = codes.pop(0) if len(codes) > 1 else codes[0]
            out = self.volume + "\n" if verb == "volume-path" and code == 0 else ""
        return subprocess.CompletedProcess(argv, code, out, "")

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


def _run_with(runner: _Runner, *, helper: bool):
    # Legacy runs use the real marker check (absent on a test machine).
    marker = (
        patch(f"{_MODULE}.uses_helper", return_value=True, create=True)
        if helper
        else contextlib.nullcontext()
    )
    return (
        patch(f"{_MODULE}.subprocess.run", runner),
        marker,
        patch(f"{_MODULE}.get_compose_project_name", return_value="dango-ab12cd34"),
    )


def _floor(verb: str) -> int:
    return ch.ROOT_LOCK_WAIT + ch.VERB_BUDGET[verb] + ch.CALLER_MARGIN


class TestHelperStopStart:
    def test_stop_order_metabase_web_metabase(self) -> None:
        runner = _Runner()
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3:
            sb._stop_services()
        assert runner.events() == ["helper:stop metabase", "web:stop", "helper:stop metabase"]
        assert not any(argv[0] == "docker" for argv, _ in runner.calls)

    def test_busy_first_stop_aborts_with_nothing_stopped(self) -> None:
        runner = _Runner({"stop metabase": 75})
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3, pytest.raises(CloudProvisioningError, match="(?i)in progress"):
            sb._stop_services()
        assert runner.events() == ["helper:stop metabase"]

    def test_busy_second_stop_restarts_dango_web(self) -> None:
        runner = _Runner({"stop metabase": [0, 75]})
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3, pytest.raises(CloudProvisioningError, match="(?i)in progress"):
            sb._stop_services()
        events = runner.events()
        assert events.index("web:stop") < events.index("web:start")

    def test_start_busy_still_starts_dango_web(self) -> None:
        runner = _Runner({"start metabase": 75})
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3:
            sb._start_services()
        assert runner.events() == ["helper:start metabase", "web:start"]

    def test_timeouts_exceed_lock_wait_plus_budget(self) -> None:
        runner = _Runner()
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3:
            sb._stop_services()
            sb._start_services()
        for argv, kwargs in runner.calls:
            if argv[0] == _HELPER:
                assert kwargs["timeout"] >= _floor(argv[1])

    def test_helper_is_called_directly_not_through_sudo(self) -> None:
        runner = _Runner()
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3:
            sb._stop_services()
        assert all(argv[0] != "sudo" for argv, _ in runner.calls)


class TestHelperArchive:
    def test_busy_stop_never_restarts_anything(self, tmp_path: Path) -> None:
        runner = _Runner({"stop metabase": 75})
        p1, p2, p3 = _run_with(runner, helper=True)
        with (
            p1,
            p2,
            p3,
            patch(f"{_MODULE}.BACKUP_DIR", tmp_path),
            pytest.raises(CloudProvisioningError),
        ):
            sb._create_local_archive("scheduled")
        assert runner.events() == ["helper:stop metabase"]


class TestVolumePath:
    def test_uses_the_helper_verb(self) -> None:
        runner = _Runner(volume="/var/lib/docker/volumes/x/_data")
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3:
            assert sb._get_metabase_volume_path() == "/var/lib/docker/volumes/x/_data"
        assert [a for a, _ in runner.calls] == [[_HELPER, "volume-path"]]
        assert runner.calls[0][1]["timeout"] >= _floor("volume-path")

    def test_failed_lookup_is_none(self) -> None:
        runner = _Runner({"volume-path": 1})
        p1, p2, p3 = _run_with(runner, helper=True)
        with p1, p2, p3:
            assert sb._get_metabase_volume_path() is None


class TestLegacyUnchanged:
    def test_service_commands(self) -> None:
        runner = _Runner()
        p1, p2, p3 = _run_with(runner, helper=False)
        with p1, p2, p3:
            sb._stop_services()
            sb._start_services()
        assert [a for a, _ in runner.calls] == [
            ["systemctl", "stop", "dango-web"],
            [
                "docker",
                "compose",
                "-f",
                "/srv/dango/project/docker-compose.yml",
                "stop",
                "metabase",
            ],
            [
                "docker",
                "compose",
                "-f",
                "/srv/dango/project/docker-compose.yml",
                "start",
                "metabase",
            ],
            ["systemctl", "start", "dango-web"],
        ]

    def test_volume_lookup(self) -> None:
        runner = _Runner()
        p1, p2, p3 = _run_with(runner, helper=False)
        with p1, p2, p3:
            sb._get_metabase_volume_path()
        assert runner.calls[0][0] == [
            "docker",
            "volume",
            "inspect",
            "dango-ab12cd34_metabase-data",
            "--format",
            "{{.Mountpoint}}",
        ]
