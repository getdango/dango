"""tests/unit/test_notebook_snapshot.py

Tests for dango.notebooks.snapshot — DuckDB snapshot management.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestCreateSnapshot:
    def test_creates_snapshot_file(self, tmp_path):
        warehouse = tmp_path / "data" / "warehouse.duckdb"
        warehouse.parent.mkdir(parents=True)
        warehouse.write_bytes(b"fake duckdb content")

        from dango.notebooks.snapshot import create_snapshot

        result = create_snapshot(tmp_path, username="alice")

        assert result.exists()
        assert result.name.startswith("warehouse_alice_")
        assert result.name.endswith(".duckdb")
        assert result.read_bytes() == b"fake duckdb content"

    def test_raises_if_warehouse_missing(self, tmp_path):
        from dango.notebooks.snapshot import create_snapshot

        with pytest.raises(FileNotFoundError, match="Warehouse database not found"):
            create_snapshot(tmp_path, username="alice")

    def test_cleanup_before_create(self, tmp_path):
        warehouse = tmp_path / "data" / "warehouse.duckdb"
        warehouse.parent.mkdir(parents=True)
        warehouse.write_bytes(b"data")

        snapshots_dir = tmp_path / ".dango" / "snapshots"
        snapshots_dir.mkdir(parents=True)

        # Create 4 pre-existing snapshots
        for i in range(4):
            (snapshots_dir / f"warehouse_alice_20260101_00000{i}.duckdb").write_bytes(b"old")

        from dango.notebooks.snapshot import create_snapshot

        create_snapshot(tmp_path, username="alice")

        all_snaps = list(snapshots_dir.glob("warehouse_alice_*.duckdb"))
        # keep=3 removes oldest, then creates 1 new = 4 total
        assert len(all_snaps) == 4


@pytest.mark.unit
class TestListSnapshots:
    def test_lists_snapshots(self, tmp_path):
        snapshots_dir = tmp_path / ".dango" / "snapshots"
        snapshots_dir.mkdir(parents=True)
        (snapshots_dir / "warehouse_alice_20260101_120000.duckdb").write_bytes(b"a")
        (snapshots_dir / "warehouse_bob_20260102_120000.duckdb").write_bytes(b"b")

        from dango.notebooks.snapshot import list_snapshots

        result = list_snapshots(tmp_path)
        assert len(result) == 2

        result_alice = list_snapshots(tmp_path, username="alice")
        assert len(result_alice) == 1
        assert result_alice[0]["username"] == "alice"

    def test_empty_dir(self, tmp_path):
        from dango.notebooks.snapshot import list_snapshots

        result = list_snapshots(tmp_path)
        assert result == []

    def test_sorted_newest_first(self, tmp_path):
        snapshots_dir = tmp_path / ".dango" / "snapshots"
        snapshots_dir.mkdir(parents=True)
        (snapshots_dir / "warehouse_alice_20260101_120000.duckdb").write_bytes(b"old")
        (snapshots_dir / "warehouse_alice_20260201_120000.duckdb").write_bytes(b"new")

        from dango.notebooks.snapshot import list_snapshots

        result = list_snapshots(tmp_path, username="alice")
        assert result[0]["created_at"] > result[1]["created_at"]


@pytest.mark.unit
class TestCleanupSnapshots:
    def test_removes_oldest_beyond_keep(self, tmp_path):
        snapshots_dir = tmp_path / ".dango" / "snapshots"
        snapshots_dir.mkdir(parents=True)
        for i in range(5):
            (snapshots_dir / f"warehouse_alice_2026010{i + 1}_120000.duckdb").write_bytes(b"data")

        from dango.notebooks.snapshot import cleanup_snapshots

        removed = cleanup_snapshots(tmp_path, "alice", keep=2)
        assert removed == 3
        remaining = list(snapshots_dir.glob("warehouse_alice_*.duckdb"))
        assert len(remaining) == 2


@pytest.mark.unit
class TestParseSnapshotFilename:
    def test_valid_filename(self):
        from dango.notebooks.snapshot import _parse_snapshot_filename

        result = _parse_snapshot_filename("warehouse_alice_20260101_120000.duckdb")
        assert result == ("alice", "20260101_120000")

    def test_invalid_filename(self):
        from dango.notebooks.snapshot import _parse_snapshot_filename

        assert _parse_snapshot_filename("random_file.duckdb") is None
        assert _parse_snapshot_filename("warehouse_.duckdb") is None

    def test_username_with_underscores(self):
        from dango.notebooks.snapshot import _parse_snapshot_filename

        result = _parse_snapshot_filename("warehouse_some_user_20260101_120000.duckdb")
        assert result is not None
        assert result[0] == "some_user"
        assert result[1] == "20260101_120000"
