"""tests/unit/test_scheduled_restore_auth_read.py

The scheduled (on-server) restore reads the staged ``auth.yml`` as the service user, never as
root, because the service user owns the staged tree once it has been handed over.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.backup_shell import run_spaces_restore

pytestmark = pytest.mark.unit

HAND_OVER = "chown -R"


def _restore(tmp_path: Path, staged_auth: str) -> tuple[Path, list[str]]:
    project = tmp_path / "project"
    (project / ".dango").mkdir(parents=True)
    (project / ".dango" / "auth.yml").write_text("enabled: true\nlive: 1\n")
    top = tmp_path / "backup-1"
    (top / ".dango").mkdir(parents=True)
    (top / ".dango" / "auth.yml").write_text(staged_auth)
    (top / "manifest.json").write_text("{}")
    _shim, events = run_spaces_restore(tmp_path, project, "backup-1")
    return project / ".dango" / "auth.yml", events


def test_staged_auth_is_read_only_as_the_service_user(tmp_path: Path) -> None:
    live, events = _restore(tmp_path, "enabled: true\nnew: 1\n")
    assert live.read_text() == "enabled: true\nnew: 1\n"
    reads = [e for e in events if "auth.yml" in e and (" head " in e or " test -f " in e)]
    assert reads, events
    assert all(e.startswith("run: runuser -u dango -- ") for e in reads), reads
    hand_over = next(i for i, e in enumerate(events) if HAND_OVER in e)
    root_touch = [
        e
        for e in events[hand_over:]
        if "auth.yml" in e and not e.startswith("run: runuser -u dango -- ") and "cp " not in e
    ]
    assert root_touch == [], root_touch


def test_staged_auth_that_turns_auth_off_is_still_refused(tmp_path: Path) -> None:
    live, _events = _restore(tmp_path, "enabled: false\n")
    assert live.read_text() == "enabled: true\nlive: 1\n"


def test_oversized_staged_auth_is_still_refused(tmp_path: Path) -> None:
    live, _events = _restore(tmp_path, "enabled: true\n" + "x: 1\n" * 20000)
    assert live.read_text() == "enabled: true\nlive: 1\n"
