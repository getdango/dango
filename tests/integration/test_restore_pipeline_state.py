"""tests/integration/test_restore_pipeline_state.py

After a restore replaces the warehouse, an incremental dlt source reloads the rows loaded since the backup.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest
from click.testing import CliRunner

_SYNC = """
import os, sys
import dlt
project, upto = sys.argv[1], int(sys.argv[2])
os.environ["DLT_DATA_DIR"] = os.path.join(project, ".dlt")
os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
rows = [{"id": i} for i in range(1, upto + 1)]

@dlt.resource(name="items", write_disposition="append")
def items(cursor=dlt.sources.incremental("id", initial_value=0)):
    yield [r for r in rows if r["id"] > cursor.last_value]

pipeline = dlt.pipeline(
    "p",
    destination=dlt.destinations.duckdb(os.path.join(project, "data", "warehouse.duckdb")),
    dataset_name="raw",
)
pipeline.run(items())
"""


def _sync(project: Path, upto: int) -> None:
    subprocess.run([sys.executable, "-c", _SYNC, str(project), str(upto)], check=True)


def _ids(project: Path) -> list[int]:
    conn = duckdb.connect(str(project / "data" / "warehouse.duckdb"), read_only=True)
    try:
        return sorted(r[0] for r in conn.execute("SELECT id FROM raw.items").fetchall())
    finally:
        conn.close()


@pytest.mark.integration
def test_incremental_source_reloads_rows_loaded_after_the_backup(tmp_path: Path) -> None:
    from dango.cli.commands.local_backup import backup_group

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    (project / ".dango").mkdir()
    _sync(project, 3)

    content = tmp_path / "backup-20260101-000000"
    (content / "data").mkdir(parents=True)
    # What a backup holds: the warehouse, not .dlt/pipelines.
    (content / "data" / "warehouse.duckdb").write_bytes(
        (project / "data" / "warehouse.duckdb").read_bytes()
    )
    archive = tmp_path / "backup-20260101-000000.tar.gz"
    with tarfile.open(archive, mode="w:gz") as tf:
        tf.add(str(content), arcname=content.name)

    _sync(project, 6)
    assert _ids(project) == [1, 2, 3, 4, 5, 6]

    with patch("dango.cli.commands.local_backup.require_project_context", return_value=project):
        result = CliRunner().invoke(
            backup_group, ["restore", str(archive), "--yes"], obj={"project_root": project}
        )
    assert result.exit_code == 0, result.output
    assert _ids(project) == [1, 2, 3]

    _sync(project, 8)
    assert _ids(project) == [1, 2, 3, 4, 5, 6, 7, 8]
