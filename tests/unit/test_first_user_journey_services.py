"""tests/unit/test_first_user_journey_services.py

In-process tests for scripts/journey_services.py and scripts/journey_support.py: the web log is
closed and written even when stopping the child fails, and the output decoding rule.
"""

import subprocess
from pathlib import Path
from types import ModuleType
from typing import IO, Any

import pytest

pytestmark = pytest.mark.unit

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, ModuleType]:
    monkeypatch.syspath_prepend(str(SCRIPTS))
    import journey_services
    import journey_support

    return journey_support, journey_services


class _UnkillableProc:
    """A child that ignores terminate() and whose wait() times out even after kill()."""

    returncode = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.stdout: IO[bytes] = kwargs["stdout"]
        self.stdout.write(b"web child output line\n")
        self.stdout.flush()
        self.killed = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("fake", timeout or 0)


def test_web_log_is_closed_and_written_when_kill_does_not_stop_the_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    support, services = _load(monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    journey = support.Journey("fake-dango", "python", work, tmp_path / "j.log", 30, False)
    created: list[_UnkillableProc] = []

    def fake_popen(*args: Any, **kwargs: Any) -> _UnkillableProc:
        proc = _UnkillableProc(*args, **kwargs)
        created.append(proc)
        return proc

    monkeypatch.setattr(services.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(services, "_http_up", lambda port: True)

    reason = services._run_web_once(journey, 12345, "first start", 0.01)

    assert created and created[0].killed
    assert created[0].stdout.closed
    assert reason is not None and "did not exit after kill" in reason
    log = (tmp_path / "j.log").read_text(encoding="utf-8")
    assert "[web first start output]" in log and "web child output line" in log


@pytest.mark.parametrize(
    ("legacy_stdio", "utf8_mode", "expected"),
    [
        (False, False, "utf-8"),
        (False, True, "utf-8"),
        (True, True, "utf-8"),
        (True, False, "cp1252"),
    ],
)
def test_child_output_is_decoded_with_the_encoding_the_child_used(
    monkeypatch: pytest.MonkeyPatch, legacy_stdio: bool, utf8_mode: bool, expected: str
) -> None:
    support, _ = _load(monkeypatch)
    monkeypatch.setattr(support.locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")
    journey = support.Journey(
        "d", "p", Path("."), Path("l"), 1, False, legacy_stdio=legacy_stdio, utf8_mode=utf8_mode
    )
    assert journey.output_encoding() == expected
