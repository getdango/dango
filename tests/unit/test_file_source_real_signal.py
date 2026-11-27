"""tests/unit/test_file_source_real_signal.py

WP-64: a real SIGINT / SIGTERM sent to a child process in the middle of a file load (real DuckDB).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import duckdb
import pytest

import dango
from dango.config.models import (
    CSVSourceConfig,
    DataSource,
    LocalFilesSourceConfig,
    SourceType,
)
from dango.ingestion.dlt_runner import DltPipelineRunner

NAME = "orders"
SCHEMA = f"raw_{NAME}"
_TABLES_SQL = f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{SCHEMA}'"

# The child interrupts nothing itself: it only announces (marker file) that a slow DuckDB query
# for big.csv is running, and waits there for the parent's real signal.
CHILD = """
import json, os, signal, sys, threading, time
from pathlib import Path
from dango.config.models import CSVSourceConfig, DataSource, LocalFilesSourceConfig, SourceType
from dango.ingestion import csv_loader
from dango.ingestion.dlt_runner import DltPipelineRunner

root, marker, kind, refresh = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4] == "1"
watchdog = threading.Timer(120, lambda: os._exit(97))
watchdog.daemon = True
watchdog.start()
signal.signal(signal.SIGINT, signal.default_int_handler)

original = csv_loader.CSVLoader._build_insert_select

def patched(self, conn, temp_table, target_table):
    name = conn.execute(f"SELECT _dango_filename FROM {temp_table} LIMIT 1").fetchone()[0]
    if name == "big.csv":
        conn.execute("SET threads=1")
        conn.execute("SET enable_progress_bar=true")
        conn.execute("SET enable_progress_bar_print=false")
        done = threading.Event()

        def announce_when_running():
            # DuckDB can drop a SIGINT that arrives as a query starts, so announce only once the
            # query reports progress (it is running)
            try:
                while not done.is_set() and conn.query_progress() < 0:
                    time.sleep(0.01)
                if not done.is_set():
                    marker.write_text("in-load", encoding="utf-8")
            except Exception:
                return

        threading.Thread(target=announce_when_running, daemon=True).start()
        try:
            conn.execute(
                "SELECT count(*) FROM range(2000000000) a(x), range(1000) b(y) "
                "WHERE (x * y) % 7 = 1"
            ).fetchall()
        finally:
            done.set()
    return original(self, conn, temp_table, target_table)

csv_loader.CSVLoader._build_insert_select = patched
uploads = root / "uploads"
if kind == "csv":
    src = DataSource(name="orders", type=SourceType.CSV,
                     csv=CSVSourceConfig(directory=uploads, file_pattern="*.csv"))
else:
    src = DataSource(name="orders", type=SourceType.LOCAL_FILES,
                     local_files=LocalFilesSourceConfig(directory=uploads, file_pattern="*.csv"))
result = DltPipelineRunner(root).run_source(src, full_refresh=refresh)
print("RESULT:" + json.dumps({"status": result.get("status")}))
"""


def _source(kind: str, root: Path) -> DataSource:
    if kind == "csv":
        cfg = CSVSourceConfig(directory=root / "uploads", file_pattern="*.csv")
        return DataSource(name=NAME, type=SourceType.CSV, csv=cfg)
    cfg = LocalFilesSourceConfig(directory=root / "uploads", file_pattern="*.csv")
    return DataSource(name=NAME, type=SourceType.LOCAL_FILES, local_files=cfg)


def _query(db: Path, sql: str) -> list[tuple]:
    conn = duckdb.connect(str(db), config={"access_mode": "read_only"})
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _run_child_and_signal(root: Path, kind: str, refresh: bool, sig: int) -> tuple[int, str]:
    marker = root / "marker.txt"
    script = root / "child_script.py"
    script.write_text(CHILD, encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(dango.__file__).resolve().parents[1]),
        "DO_NOT_TRACK": "1",
        "DANGO_TELEMETRY": "false",
        "RUNTIME__DLTHUB_TELEMETRY": "false",
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
    }
    proc = subprocess.Popen(
        [sys.executable, str(script), str(root), str(marker), kind, "1" if refresh else "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        deadline = time.monotonic() + 90
        while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)  # waiting for the child's marker, not ordering anything
        assert marker.exists(), "child never reached the load"
        proc.send_signal(sig)
        out, _ = proc.communicate(timeout=60)
        return proc.returncode, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


@pytest.mark.parametrize("kind", ["csv", "local_files"])
@pytest.mark.parametrize(
    ("sig", "refresh"),
    [(signal.SIGINT, True), (signal.SIGTERM, True), (signal.SIGINT, False)],
)
def test_real_signal_in_a_file_load_is_an_interrupt_and_keeps_the_previous_data(
    kind, sig, refresh, tmp_path
):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "a.csv").write_text("id,item\n1,a\n2,b\n", encoding="utf-8")
    (uploads / "b.csv").write_text("id,item\n3,c\n", encoding="utf-8")
    runner = DltPipelineRunner(tmp_path)
    fn = runner._run_csv_source if kind == "csv" else runner._run_local_files_source
    assert fn(_source(kind, tmp_path))["status"] == "success"
    before_rows = _query(runner.duckdb_path, f"SELECT * FROM {SCHEMA}.{NAME} ORDER BY id")
    before_meta = _query(runner.duckdb_path, "SELECT * FROM _dango_file_metadata ORDER BY 2")
    (uploads / "big.csv").write_text("id,item\n4,d\n", encoding="utf-8")

    code, out = _run_child_and_signal(tmp_path, kind, refresh, sig)

    results = [ln for ln in out.splitlines() if ln.startswith("RESULT:")]
    assert code == 0, out[-2000:]
    assert json.loads(results[-1][len("RESULT:") :]) == {"status": "interrupted"}
    tables = {t for (t,) in _query(runner.duckdb_path, _TABLES_SQL)}
    assert tables == {NAME}  # no backup table, no temp table left behind
    assert not _query(
        runner.duckdb_path,
        "SELECT table_name FROM information_schema.tables WHERE table_name LIKE '\\_temp\\_%' ESCAPE '\\'",
    )
    assert _query(runner.duckdb_path, f"SELECT * FROM {SCHEMA}.{NAME} ORDER BY id") == before_rows
    assert _query(runner.duckdb_path, "SELECT * FROM _dango_file_metadata ORDER BY 2") == (
        before_meta
    )
