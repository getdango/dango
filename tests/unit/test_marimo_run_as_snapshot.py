"""tests/unit/test_marimo_run_as_snapshot.py

Isolated snapshots and the restart route for the restricted-user Marimo launcher.
"""

from __future__ import annotations

import asyncio
import inspect
import stat
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.notebooks import snapshot

_ISO = "dango.platform.run_as.uses_isolation"


# --- snapshot ---------------------------------------------------------------


def _warehouse(root: Path, mode: int = 0o644) -> Path:
    wh = root / "data" / "warehouse.duckdb"
    wh.parent.mkdir(parents=True)
    wh.write_bytes(b"duck")
    wh.chmod(mode)
    return wh


def test_isolated_snapshot_dir_and_mode(tmp_path: Path) -> None:
    _warehouse(tmp_path, 0o644)
    snaps = tmp_path / "runner-snaps"
    snaps.mkdir()
    with patch(_ISO, return_value=True), patch.object(snapshot, "ISOLATED_SNAPSHOT_DIR", snaps):
        path = snapshot.create_snapshot(tmp_path, "bob")
    assert path.parent == snaps and path.read_bytes() == b"duck"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not (tmp_path / ".dango" / "snapshots").exists()


def test_isolated_snapshot_missing_dir_fails_clearly(tmp_path: Path) -> None:
    _warehouse(tmp_path)
    missing = tmp_path / "nope"
    with (
        patch(_ISO, return_value=True),
        patch.object(snapshot, "ISOLATED_SNAPSHOT_DIR", missing),
        pytest.raises(RuntimeError, match="snapshot"),
    ):
        snapshot.create_snapshot(tmp_path, "bob")
    assert not missing.exists()


def test_isolated_snapshot_keeps_three_per_user(tmp_path: Path) -> None:
    _warehouse(tmp_path)
    snaps = tmp_path / "s"
    snaps.mkdir()
    for i in range(5):
        (snaps / f"warehouse_bob_2026010{i + 1}_000000.duckdb").write_bytes(b"x")
    with patch(_ISO, return_value=True), patch.object(snapshot, "ISOLATED_SNAPSHOT_DIR", snaps):
        snapshot.create_snapshot(tmp_path, "bob")
        assert len(snapshot.list_snapshots(tmp_path, "bob")) == 4  # 3 kept + the new one


def test_isolated_snapshot_refuses_symlinked_warehouse(tmp_path: Path) -> None:
    wh = _warehouse(tmp_path)
    target = tmp_path / "other.duckdb"
    target.write_bytes(b"x")
    wh.unlink()
    wh.symlink_to(target)
    snaps = tmp_path / "s"
    snaps.mkdir()
    with (
        patch(_ISO, return_value=True),
        patch.object(snapshot, "ISOLATED_SNAPSHOT_DIR", snaps),
        pytest.raises(OSError),
    ):
        snapshot.create_snapshot(tmp_path, "bob")
    assert list(snaps.iterdir()) == []


def test_isolated_copy_code_has_no_copy2_or_copymode() -> None:
    src = inspect.getsource(snapshot._create_isolated_snapshot)
    for banned in ("copy2", "copymode", "copystat", "chgrp", "shutil.copy("):
        assert banned not in src


def test_non_isolated_snapshot_unchanged(tmp_path: Path) -> None:
    _warehouse(tmp_path, 0o600)
    with patch(_ISO, return_value=False):
        path = snapshot.create_snapshot(tmp_path, "bob")
    assert path.parent == tmp_path / ".dango" / "snapshots"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600  # copy2 keeps the source mode


def test_two_isolated_snapshots_in_the_same_second_both_succeed(tmp_path: Path) -> None:
    _warehouse(tmp_path)
    snaps = tmp_path / "s"
    snaps.mkdir()
    fake = MagicMock(wraps=datetime)
    fake.now.return_value = datetime(2026, 10, 25, 12, 0, 0)
    with (
        patch(_ISO, return_value=True),
        patch.object(snapshot, "ISOLATED_SNAPSHOT_DIR", snaps),
        patch.object(snapshot, "datetime", fake),
    ):
        first = snapshot.create_snapshot(tmp_path, "bob")
        second = snapshot.create_snapshot(tmp_path, "bob")
        listed = snapshot.list_snapshots(tmp_path, "bob")
    assert first != second and first.exists() and second.exists()
    assert len(listed) == 2
    assert stat.S_IMODE(second.stat().st_mode) == 0o640


# --- restart route ----------------------------------------------------------


def _restart(
    tmp_path: Path,
    *,
    stopped: bool,
    running: bool,
    isolated: bool = False,
    on_loop: list[bool] | None = None,
) -> tuple[Any, int]:
    from tests.unit.test_web_notebooks import _client, _init_db, _seed_lock

    _init_db(tmp_path)
    _seed_lock(tmp_path, "nb1")
    route = "dango.web.routes.notebooks"

    def _stop(_root: Path) -> bool:
        if on_loop is not None:
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
        return stopped

    with (
        patch(_ISO, return_value=isolated),
        patch(f"{route}.get_project_root", return_value=tmp_path),
        patch(f"{route}.stop_marimo", side_effect=_stop),
        patch(f"{route}.get_marimo_status", return_value={"running": running}),
    ):
        resp = _client(tmp_path).post("/api/notebooks/marimo/restart")
    from dango.utils.dango_db import connect

    with connect(tmp_path) as conn:
        locks = conn.execute("SELECT COUNT(*) AS c FROM notebook_locks").fetchone()["c"]
    return resp, locks


def test_isolated_restart_reports_conflict_and_keeps_locks_when_marimo_cannot_stop(
    tmp_path: Path,
) -> None:
    resp, locks = _restart(tmp_path, stopped=False, running=True, isolated=True)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "DANGO-NB012"
    assert locks == 1


def test_non_isolated_restart_releases_locks_even_when_the_stop_failed(tmp_path: Path) -> None:
    resp, locks = _restart(tmp_path, stopped=False, running=True, isolated=False)
    assert resp.status_code == 200 and resp.json()["restarted"] is True
    assert locks == 0


def test_isolated_restart_succeeds_when_marimo_was_not_running(tmp_path: Path) -> None:
    resp, locks = _restart(tmp_path, stopped=False, running=False, isolated=True)
    assert resp.status_code == 200 and resp.json()["restarted"] is True
    assert locks == 0


def test_restart_does_not_stop_marimo_on_the_event_loop(tmp_path: Path) -> None:
    on_loop: list[bool] = []
    _restart(tmp_path, stopped=True, running=False, on_loop=on_loop)
    assert on_loop == [False]
