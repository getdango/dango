"""tests/unit/test_duckdb_capacity.py

Unit tests for ``get_duckdb_capacity()`` in ``dango/utils/db_health.py``.
"""

from __future__ import annotations

from collections import namedtuple
from unittest.mock import patch

import pytest

from dango.utils.db_health import get_duckdb_capacity

_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])


def _make_vmem(total: int):
    """Build a minimal virtual_memory mock."""
    m = type("vmem", (), {"total": total})()
    return m


@pytest.mark.unit
class TestDuckDBCapacity:
    def test_capacity_healthy(self, tmp_path):
        """DB at ~10% returns healthy status, warning=False."""
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.write_bytes(b"\x00" * 100)  # 100 bytes

        # RAM=1 GB, disk free=10 GB → recommended_max = min(4GB, 8GB) = 4 GB
        with (
            patch("dango.utils.db_health.shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_vmem,
        ):
            mock_disk.return_value = _DiskUsage(
                total=20 * 1024**3, used=10 * 1024**3, free=10 * 1024**3
            )
            mock_vmem.return_value = _make_vmem(1 * 1024**3)

            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["duckdb_size_bytes"] == 100
        assert result["duckdb_capacity_status"] == "healthy"
        assert result["duckdb_capacity_warning"] is False
        assert result["duckdb_capacity_pct"] < 50

    def test_capacity_warning(self, tmp_path):
        """DB at ~60% returns warning status, warning=False."""
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)

        # RAM=1 GB → recommended_max = min(4GB, 8GB) = 4 GB
        # DB at 60% of 4 GB = 2.4 GB
        db_size = int(4 * 1024**3 * 0.6)
        db_file.write_bytes(b"\x00" * db_size)

        with (
            patch("dango.utils.db_health.shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_vmem,
        ):
            mock_disk.return_value = _DiskUsage(
                total=20 * 1024**3, used=10 * 1024**3, free=10 * 1024**3
            )
            mock_vmem.return_value = _make_vmem(1 * 1024**3)

            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["duckdb_capacity_status"] == "warning"
        assert result["duckdb_capacity_warning"] is False
        assert 50 < result["duckdb_capacity_pct"] <= 75

    def test_capacity_critical(self, tmp_path):
        """DB at ~80% returns critical status, warning=True."""
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)

        # RAM=1 GB → recommended_max = min(4GB, 8GB) = 4 GB
        # DB at 80% of 4 GB = 3.2 GB
        db_size = int(4 * 1024**3 * 0.8)
        db_file.write_bytes(b"\x00" * db_size)

        with (
            patch("dango.utils.db_health.shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_vmem,
        ):
            mock_disk.return_value = _DiskUsage(
                total=20 * 1024**3, used=10 * 1024**3, free=10 * 1024**3
            )
            mock_vmem.return_value = _make_vmem(1 * 1024**3)

            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["duckdb_capacity_status"] == "critical"
        assert result["duckdb_capacity_warning"] is True
        assert result["duckdb_capacity_pct"] > 75

    def test_no_db_file(self, tmp_path):
        """Non-existent DB file returns 0 bytes, 0%."""
        db_file = tmp_path / "data" / "warehouse.duckdb"

        with (
            patch("dango.utils.db_health.shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_vmem,
        ):
            mock_disk.return_value = _DiskUsage(
                total=20 * 1024**3, used=10 * 1024**3, free=10 * 1024**3
            )
            mock_vmem.return_value = _make_vmem(1 * 1024**3)

            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["duckdb_size_bytes"] == 0
        assert result["duckdb_capacity_pct"] == 0.0
        assert result["duckdb_capacity_status"] == "healthy"

    def test_psutil_failure(self, tmp_path):
        """psutil failure returns safe fallback."""
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.write_bytes(b"\x00" * 100)

        with patch("psutil.virtual_memory", side_effect=OSError("no mem")):
            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["duckdb_size_bytes"] == 0
        assert result["duckdb_capacity_status"] == "unknown"
        assert result["duckdb_capacity_warning"] is False

    def test_recommended_max_uses_minimum(self, tmp_path):
        """recommended_max = min(RAM*4, disk_free*0.8)."""
        db_file = tmp_path / "data" / "warehouse.duckdb"
        db_file.parent.mkdir(parents=True)
        db_file.write_bytes(b"\x00" * 100)

        # Case 1: RAM*4 < disk*0.8 → RAM*4 wins
        with (
            patch("dango.utils.db_health.shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_vmem,
        ):
            mock_disk.return_value = _DiskUsage(total=100 * 1024**3, used=0, free=100 * 1024**3)
            mock_vmem.return_value = _make_vmem(1 * 1024**3)  # RAM*4 = 4 GB

            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["recommended_max_db_size_bytes"] == 4 * 1024**3

        # Case 2: disk*0.8 < RAM*4 → disk*0.8 wins
        with (
            patch("dango.utils.db_health.shutil.disk_usage") as mock_disk,
            patch("psutil.virtual_memory") as mock_vmem,
        ):
            mock_disk.return_value = _DiskUsage(
                total=10 * 1024**3, used=8 * 1024**3, free=2 * 1024**3
            )
            mock_vmem.return_value = _make_vmem(8 * 1024**3)  # RAM*4 = 32 GB

            result = get_duckdb_capacity(db_file, tmp_path)

        assert result["recommended_max_db_size_bytes"] == int(2 * 1024**3 * 0.8)
