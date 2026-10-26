"""tests/unit/test_metadata_bootstrap.py

Verify serialized metadata bootstrap using independent temporary SQLite projects.
"""

from __future__ import annotations

import importlib
import math
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock, call, patch

import pytest

from dango.exceptions import MigrationApplicationError
from dango.migrations.metadata.bootstrap import ensure_metadata_schema
from dango.utils import dango_db

pytestmark = pytest.mark.unit
JOURNAL = {"platform_operations", "platform_effects", "platform_leases"}
DESCRIPTION = "Create platform operation, effect and lease journal"
WORKTREE = Path(__file__).resolve().parents[2]

# Every child imports the actual worktree and opens its own SQLite connections.
WORKER = """
import os
import sys
import time
from pathlib import Path
from dango.migrations.runner import MigrationRunner
from dango.utils.dango_db import get_connection

project, role = Path(sys.argv[1]), sys.argv[2]
original = MigrationRunner.apply_one
def observed(self, migration):
    with (project / 'applications').open('a') as log:
        log.write(str(migration.version) + '\\n')
    if role == 'holder':
        (project / 'held').touch()
        deadline = time.monotonic() + 15
        while not (project / 'release').exists():
            if time.monotonic() > deadline:
                raise RuntimeError('parent did not release migration')
            time.sleep(0.01)
    return original(self, migration)
MigrationRunner.apply_one = observed
(project / (role + '-started')).touch()
conn = get_connection(project)
assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
conn.close()
"""

SIMULTANEOUS_WORKER = """
import sqlite3
import sys
import time
from pathlib import Path
from dango.utils.dango_db import get_connection
project, role = Path(sys.argv[1]), sys.argv[2]
(project / (role + '-ready')).touch()
deadline = time.monotonic() + 10
while not (project / 'go').exists():
    assert time.monotonic() < deadline
    time.sleep(0.001)
conn = get_connection(project)
assert conn.row_factory is sqlite3.Row
assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
assert conn.execute('PRAGMA busy_timeout').fetchone()[0] == 5000
assert not conn.in_transaction
conn.close()
"""


def _wait_for(path: Path) -> None:
    """Wait only for a bounded child-process handshake."""
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() >= deadline:
            pytest.fail(f"Child did not create {path.name}")
        time.sleep(0.01)


def _start(project: Path, role: str, *, worker: str = WORKER) -> subprocess.Popen[str]:
    """Start an independent Python process against a synthetic project."""
    return subprocess.Popen(
        [sys.executable, "-c", worker, str(project), role],
        cwd=WORKTREE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _finish(child: subprocess.Popen[str]) -> None:
    """Reap children, including when an assertion fails."""
    if child.poll() is None:
        child.kill()
    child.communicate(timeout=10)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_two_processes_initialize_once(tmp_path: Path) -> None:
    first = _start(tmp_path, "holder")
    second = None
    try:
        _wait_for(tmp_path / "held")
        second = _start(tmp_path, "waiter")
        _wait_for(tmp_path / "waiter-started")
        time.sleep(0.15)
        assert second.poll() is None
        assert (tmp_path / "applications").read_text().splitlines() == ["1"]
        (tmp_path / "release").touch()
        for child in (first, second):
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stdout + stderr
        with sqlite3.connect(tmp_path / ".dango/dango.db") as conn:
            assert JOURNAL <= _tables(conn)
            assert conn.execute("SELECT version, description FROM _migrations").fetchall() == [
                (1, DESCRIPTION)
            ]
        assert (tmp_path / "applications").read_text().splitlines() == ["1"]
        assert not (tmp_path / ".dango/metadata.db").exists()
    finally:
        _finish(first)
        if second is not None:
            _finish(second)


@pytest.mark.parametrize("pair", range(3))
def test_simultaneous_fresh_first_opens_succeed(tmp_path: Path, pair: int) -> None:
    project = tmp_path / str(pair)
    project.mkdir()
    children = []
    try:
        for role in ("first", "second"):
            children.append(_start(project, role, worker=SIMULTANEOUS_WORKER))
        for role in ("first", "second"):
            _wait_for(project / f"{role}-ready")
        assert not (project / ".dango").exists()
        (project / "go").touch()
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stdout + stderr
        with sqlite3.connect(project / ".dango/dango.db") as conn:
            assert conn.execute("SELECT version, description FROM _migrations").fetchall() == [
                (1, DESCRIPTION)
            ]
            assert JOURNAL <= _tables(conn)
        assert not (project / ".dango/metadata.db").exists()
    finally:
        for child in children:
            _finish(child)


def test_wal_retry_is_bounded_and_specific() -> None:
    conn = Mock()
    conn.execute.side_effect = [sqlite3.OperationalError("database is locked"), None]
    dango_db._enable_wal(conn, timeout_seconds=1)
    assert conn.execute.call_args_list == [call("PRAGMA journal_mode=WAL")] * 2

    conn.reset_mock()
    conn.execute.side_effect = sqlite3.OperationalError("database is locked")
    started = time.monotonic()
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        dango_db._enable_wal(conn, timeout_seconds=0.03)
    assert 0.02 <= time.monotonic() - started < 1
    assert conn.execute.call_count >= 2

    conn.reset_mock()
    conn.execute.side_effect = sqlite3.OperationalError("disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        dango_db._enable_wal(conn)
    conn.execute.assert_called_once_with("PRAGMA journal_mode=WAL")


def test_wal_setup_failure_closes_caller(tmp_path: Path) -> None:
    conn = Mock()
    with patch.object(sqlite3, "connect", return_value=conn) as open_db:
        with patch.object(
            dango_db, "_enable_wal", side_effect=sqlite3.OperationalError("database is locked")
        ):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                dango_db.get_connection(tmp_path)
    open_db.assert_called_once_with(str(tmp_path / ".dango/dango.db"), timeout=0)
    conn.close.assert_called_once_with()
    conn.execute.assert_not_called()
    assert str(tmp_path / ".dango/dango.db") not in dango_db._schema_initialized


def test_bootstrap_failure_releases_lock_and_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner_module = importlib.import_module("dango.migrations.runner")
    original_load = runner_module._load_migration_module
    opened = []
    original_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def track(*args, **kwargs):
        conn = original_connect(*args, **kwargs, factory=TrackedConnection)
        opened.append(conn)
        return conn

    def failing_load(path):
        module = original_load(path)

        def upgrade(conn):
            conn.execute("CREATE TABLE transient_bootstrap (value TEXT)")
            conn.execute("INSERT INTO schema_baselines VALUES ('bad', 't', 'c', 'TEXT', 'now')")
            raise RuntimeError("injected bootstrap failure")

        module.upgrade = upgrade
        return module

    with monkeypatch.context() as failure:
        failure.setattr(sqlite3, "connect", track)
        failure.setattr(runner_module, "_load_migration_module", failing_load)
        with pytest.raises(MigrationApplicationError, match="injected bootstrap failure"):
            dango_db.get_connection(tmp_path)
    assert opened and all(conn.closed for conn in opened)
    assert str(tmp_path / ".dango/dango.db") not in dango_db._schema_initialized
    with original_connect(tmp_path / ".dango/dango.db") as conn:
        assert not JOURNAL & _tables(conn)
        assert "transient_bootstrap" not in _tables(conn)
        assert conn.execute("SELECT * FROM schema_baselines").fetchall() == []
        assert conn.execute("SELECT * FROM _migrations").fetchall() == []
    # Zero-wait retry also proves the failed invocation released its flock.
    ensure_metadata_schema(tmp_path, timeout_seconds=0)
    conn = dango_db.get_connection(tmp_path)
    try:
        assert conn.execute("SELECT version FROM _migrations").fetchall()[0][0] == 1
        assert JOURNAL <= _tables(conn)
    finally:
        conn.close()


def test_timeout_is_actionable_without_half_schema(tmp_path: Path) -> None:
    import fcntl

    directory = tmp_path / ".dango"
    directory.mkdir()
    with (directory / "metadata-migration.lock").open("a+b") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(MigrationApplicationError, match="Timed out") as error:
            ensure_metadata_schema(tmp_path, timeout_seconds=0.1)
        assert 0.09 <= time.monotonic() - started < 2
        assert "retry" in str(error.value) and "timeout_seconds" in str(error.value)
        assert "Do not delete" in str(error.value)
        assert not (directory / "dango.db").exists()
    ensure_metadata_schema(tmp_path, timeout_seconds=0)
    with sqlite3.connect(directory / "dango.db") as conn:
        assert JOURNAL <= _tables(conn)
        assert conn.execute("SELECT version FROM _migrations").fetchall() == [(1,)]


def test_legacy_rows_and_connection_pragmas_preserved(tmp_path: Path) -> None:
    directory = tmp_path / ".dango"
    directory.mkdir()
    # Independent pre-upgrade fixture, without calling a bootstrap/setup helper.
    with sqlite3.connect(directory / "dango.db") as conn:
        conn.execute(
            "CREATE TABLE schema_baselines (source TEXT, table_name TEXT, column_name TEXT, "
            "data_type TEXT, captured_at TEXT, PRIMARY KEY (source, table_name, column_name))"
        )
        conn.execute(
            "INSERT INTO schema_baselines VALUES ('legacy', 'orders', 'id', 'INT', 'then')"
        )
    siblings = [directory / f"{name}.db" for name in ("auth", "scheduler")]
    for path in siblings:
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE sentinel (value TEXT)")
            conn.execute("INSERT INTO sentinel VALUES ('keep')")
    sibling_bytes = [path.read_bytes() for path in siblings]
    for _ in range(2):
        conn = dango_db.get_connection(tmp_path)
        try:
            assert conn.row_factory is sqlite3.Row
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert not conn.in_transaction
            assert [tuple(row) for row in conn.execute("SELECT * FROM schema_baselines")] == [
                ("legacy", "orders", "id", "INT", "then")
            ]
            assert JOURNAL <= _tables(conn)
            assert [tuple(row) for row in conn.execute("SELECT version FROM _migrations")] == [(1,)]
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO platform_leases VALUES ('p', 'missing', 'token', 1, 0, 'now')"
                )
        finally:
            conn.close()
    assert [path.read_bytes() for path in siblings] == sibling_bytes
    assert not (directory / "metadata.db").exists()


def test_lock_inode_survives_release(tmp_path: Path) -> None:
    import fcntl

    ensure_metadata_schema(tmp_path)
    lock_path = tmp_path / ".dango/metadata-migration.lock"
    with lock_path.open("rb") as waiter:
        inode = lock_path.stat().st_ino
        ensure_metadata_schema(tmp_path)
        assert lock_path.stat().st_ino == inode
        fcntl.flock(waiter, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(MigrationApplicationError, match="Timed out"):
                ensure_metadata_schema(tmp_path, timeout_seconds=0)
        finally:
            fcntl.flock(waiter, fcntl.LOCK_UN)
    assert lock_path.exists() and lock_path.stat().st_ino == inode


def test_import_does_not_initialize(tmp_path: Path) -> None:
    script = """
import builtins
import importlib
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch
import dango.utils.dango_db
def forbidden(*args, **kwargs):
    raise AssertionError('import initialized state')
with patch.object(sqlite3, 'connect', forbidden), \\
     patch.object(Path, 'mkdir', forbidden), \\
     patch.object(Path, 'open', forbidden), \\
     patch.object(os, 'open', forbidden):
    importlib.import_module('dango.migrations.metadata.bootstrap')
    importlib.reload(dango.utils.dango_db)
assert list(Path.cwd().iterdir()) == []
"""
    # -c imports the checkout using the parent's existing package path.
    script = f"import sys; sys.path.insert(0, {str(WORKTREE)!r})\n" + script
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


def test_process_death_releases_lock(tmp_path: Path) -> None:
    child = _start(tmp_path, "holder")
    try:
        _wait_for(tmp_path / "held")
        lock_path = tmp_path / ".dango/metadata-migration.lock"
        inode = lock_path.stat().st_ino
        child.kill()
        child.communicate(timeout=10)
        ensure_metadata_schema(tmp_path, timeout_seconds=0)
        assert lock_path.stat().st_ino == inode
        with sqlite3.connect(tmp_path / ".dango/dango.db") as conn:
            assert JOURNAL <= _tables(conn)
            assert conn.execute("SELECT version FROM _migrations").fetchall() == [(1,)]
    finally:
        _finish(child)


@pytest.mark.parametrize(
    ("version", "description", "partial"),
    [
        (99, "unknown", False),
        (1, "foreign stream", False),
        (1, DESCRIPTION, False),
        (1, DESCRIPTION, True),
    ],
)
def test_unexplained_history_is_rejected_without_rewriting(
    tmp_path: Path, version: int, description: str, partial: bool
) -> None:
    directory = tmp_path / ".dango"
    directory.mkdir()
    with sqlite3.connect(directory / "dango.db") as conn:
        conn.execute(
            "CREATE TABLE _migrations (version INTEGER PRIMARY KEY, "
            "description TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO _migrations VALUES (?, ?, 'then')", (version, description))
        if partial:
            for table in JOURNAL:
                conn.execute(f"CREATE TABLE {table} (stub TEXT)")
    with pytest.raises(MigrationApplicationError, match="inspect"):
        dango_db.get_connection(tmp_path)
    with sqlite3.connect(directory / "dango.db") as conn:
        assert conn.execute("SELECT * FROM _migrations").fetchall() == [
            (version, description, "then")
        ]
        assert JOURNAL & _tables(conn) == (JOURNAL if partial else set())


def test_legacy_cache_cannot_hide_bootstrap_failure(tmp_path: Path) -> None:
    conn = dango_db.get_connection(tmp_path)
    conn.close()
    with patch(
        "dango.migrations.runner.MigrationRunner.apply_pending",
        side_effect=MigrationApplicationError("injected cached failure"),
    ) as apply:
        with pytest.raises(MigrationApplicationError, match="injected cached failure"):
            dango_db.get_connection(tmp_path)
    apply.assert_called_once()
    assert str(tmp_path / ".dango/dango.db") not in dango_db._schema_initialized
    conn = dango_db.get_connection(tmp_path)
    conn.close()


@pytest.mark.parametrize("timeout", [-1, math.inf, -math.inf, math.nan])
def test_invalid_timeout_does_not_create_state(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ensure_metadata_schema(tmp_path, timeout_seconds=timeout)
    assert list(tmp_path.iterdir()) == []
