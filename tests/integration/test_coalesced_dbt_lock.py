"""tests/integration/test_coalesced_dbt_lock.py

The scheduler's coalesced dbt run and dbt docs generation against another writer, with real dbt and
DuckDB and no Docker: a second process holds the dbt lock and a warehouse write connection, and the
run must wait for it (or report the queue message), never collide with its DuckDB file lock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import duckdb
import pytest
from click.testing import CliRunner

import dango
from dango.cli.commands.source import sync
from dango.exceptions import DbtLockError
from dango.platform.scheduling import jobs
from dango.utils.dbt_lock import DbtLock
from tests.integration.dbt_outcome_support import make_csv_project

pytestmark = pytest.mark.integration

HOLDER = Path(__file__).with_name("lock_holder_process.py")
QUEUE_MESSAGE = "Sync queue timeout. Another sync is still running."
RAW_DUCKDB_LOCK_TEXT = ("Could not set lock", "Conflicting lock", "write lock held")
CLEAN_CSV = "id,name\n1,a\n2,b\n3,c\n"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DANGO_TELEMETRY", "false")
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)
    monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A synced CSV project whose `shop` source is pending a coalesced dbt run (mart dropped)."""
    root = make_csv_project(tmp_path / "proj", CLEAN_CSV)
    monkeypatch.chdir(root)
    result = CliRunner().invoke(sync, ["--yes"], obj={"project_root": root})
    assert result.exit_code == 0, result.output
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    conn.execute("DROP TABLE marts.order_count")  # only a real dbt run brings it back
    conn.close()
    jobs._add_pending_dbt_source(root, "shop")
    monkeypatch.setattr(jobs, "_get_coalesce_seconds", lambda _root: 0)
    monkeypatch.setattr(
        "dango.visualization.metabase.refresh_metabase_connection",
        lambda *_a, **_k: (False, "not running", None),
    )
    return root


@pytest.fixture
def holder(project: Path) -> Iterator[Callable[[], None]]:
    """Another process holding the dbt lock and a warehouse write connection; yields `release`."""
    package_parent = str(Path(dango.__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([package_parent, os.environ.get("PYTHONPATH", "")]),
    }
    proc = subprocess.Popen(
        [sys.executable, str(HOLDER), str(project)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def release() -> None:
        (project / "holder.release").write_text("go", encoding="utf-8", newline="\n")

    try:
        deadline = time.monotonic() + 60
        while not (project / "holder.ready").exists():
            assert proc.poll() is None, proc.stdout.read() if proc.stdout else "holder exited"
            assert time.monotonic() < deadline, "lock holder did not start"
            time.sleep(0.05)
        yield release
    finally:
        release()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout:
            proc.stdout.close()


def _activity(root: Path) -> list[dict[str, str]]:
    path = root / ".dango" / "logs" / "activity.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _pending(root: Path) -> list[str]:
    return json.loads((root / ".dango" / "state" / "pending_dbt_sources.json").read_text("utf-8"))


def _mart_exists(root: Path) -> bool:
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"), read_only=True)
    try:
        rows = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'marts' AND table_name = 'order_count'"
        ).fetchone()
        return bool(rows and rows[0])
    finally:
        conn.close()


def test_coalesced_run_waits_for_the_writer_then_runs(
    project: Path, holder: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    # raising=False: the module constant is new, the test must fail on its behavior before it exists
    monkeypatch.setattr(jobs, "_DBT_LOCK_WAIT_SECONDS", 60, raising=False)
    # long enough that an unlocked dbt (which connects a few seconds after it starts) collides
    timer = threading.Timer(8.0, holder)
    timer.start()
    try:
        ok = jobs._run_coalesced_dbt(project)
    finally:
        timer.cancel()

    assert ok is True
    assert _pending(project) == []
    assert _mart_exists(project)
    messages = " ".join(entry["message"] for entry in _activity(project))
    assert not any(text in messages for text in RAW_DUCKDB_LOCK_TEXT)


def test_coalesced_run_reports_the_queue_message_and_keeps_its_sources(
    project: Path, holder: Callable[[], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(jobs, "_DBT_LOCK_WAIT_SECONDS", 1, raising=False)

    ok = jobs._run_coalesced_dbt(project)

    assert ok is False
    errors = [e["message"] for e in _activity(project) if e["level"] == "error"]
    assert any(QUEUE_MESSAGE in message for message in errors), errors
    assert not any(text in " ".join(errors) for text in RAW_DUCKDB_LOCK_TEXT)
    assert _pending(project) == ["shop"]  # the next run still transforms them
    assert not _mart_exists(project)  # dbt never started against the other writer's warehouse


def test_dbt_lock_is_held_while_the_coalesced_dbt_runs(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dango.transformation as transformation

    real_run = transformation.run_dbt_models
    observed: list[bool] = []

    def probe(
        root: Path, select: str | None = None, full_refresh: bool = False
    ) -> tuple[bool, str]:
        other = DbtLock(root, source="probe", operation="probe")
        try:
            other.acquire(timeout=0)
            observed.append(False)  # the lock was free while dbt was writing the warehouse
        except DbtLockError:
            observed.append(True)
        finally:
            other.release()
        return real_run(root, select=select, full_refresh=full_refresh)

    monkeypatch.setattr("dango.transformation.run_dbt_models", probe)

    assert jobs._run_coalesced_dbt(project) is True
    assert observed == [True]
    assert _mart_exists(project)


def test_a_writer_without_the_lock_makes_docs_generation_fail(
    project: Path, holder: Callable[[], None]
) -> None:
    """Why docs run under the lock: `dbt docs generate` connects to the warehouse."""
    from dango.transformation import generate_dbt_docs

    ok, output = generate_dbt_docs(project)

    assert ok is False
    assert any(text in output for text in RAW_DUCKDB_LOCK_TEXT[:2]), output


def test_docs_are_not_generated_and_warn_while_another_writer_holds_the_lock(
    project: Path, holder: Callable[[], None], caplog: pytest.LogCaptureFixture
) -> None:
    from dango.transformation.docs_locked import generate_dbt_docs_locked

    catalog = project / "dbt" / "target" / "catalog.json"
    catalog.unlink(missing_ok=True)

    with caplog.at_level("WARNING"):
        ok, message = generate_dbt_docs_locked(project, timeout=1)

    assert ok is False
    assert "busy" in message.lower()
    assert not catalog.exists()
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_docs_are_generated_under_the_lock_when_it_is_free(project: Path) -> None:
    from dango.transformation.docs_locked import generate_dbt_docs_locked

    catalog = project / "dbt" / "target" / "catalog.json"
    catalog.unlink(missing_ok=True)

    ok, _output = generate_dbt_docs_locked(project, timeout=5)

    assert ok is True
    assert catalog.exists()
    probe = DbtLock(project, source="probe", operation="probe")
    probe.acquire(timeout=0)  # released again afterwards
    probe.release()
