"""tests/unit/test_db_health_disk.py

Tests for disk monitoring utilities in dango.utils.db_health.

Covers _dir_size_bytes(), get_component_disk_usage(), and TTL cache behavior.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestDirSizeBytes:
    """Tests for _dir_size_bytes()."""

    def test_nonexistent_directory_returns_zero(self, tmp_path: Path) -> None:
        from dango.utils.db_health import _dir_size_bytes

        assert _dir_size_bytes(tmp_path / "nope") == 0

    def test_empty_directory_returns_zero(self, tmp_path: Path) -> None:
        from dango.utils.db_health import _dir_size_bytes

        empty = tmp_path / "empty"
        empty.mkdir()
        assert _dir_size_bytes(empty) == 0

    def test_single_file(self, tmp_path: Path) -> None:
        from dango.utils.db_health import _dir_size_bytes

        f = tmp_path / "data.bin"
        f.write_bytes(b"x" * 1024)
        assert _dir_size_bytes(tmp_path) == 1024

    def test_nested_files(self, tmp_path: Path) -> None:
        from dango.utils.db_health import _dir_size_bytes

        sub = tmp_path / "a" / "b"
        sub.mkdir(parents=True)
        (tmp_path / "top.txt").write_bytes(b"A" * 100)
        (sub / "deep.txt").write_bytes(b"B" * 200)
        assert _dir_size_bytes(tmp_path) == 300

    def test_oserror_on_rglob_is_handled(self, tmp_path: Path) -> None:
        """OSError during rglob traversal is silently caught."""
        from dango.utils.db_health import _dir_size_bytes

        (tmp_path / "ok.txt").write_bytes(b"x" * 50)

        def boom(*args, **kwargs):
            raise OSError("Permission denied")

        with patch.object(Path, "rglob", side_effect=boom):
            result = _dir_size_bytes(tmp_path)

        assert result == 0  # outer OSError caught, returns accumulated total

    def test_file_path_returns_zero(self, tmp_path: Path) -> None:
        """Passing a file (not a directory) returns 0."""
        from dango.utils.db_health import _dir_size_bytes

        f = tmp_path / "file.txt"
        f.write_bytes(b"data")
        assert _dir_size_bytes(f) == 0


@pytest.mark.unit
class TestGetComponentDiskUsage:
    """Tests for get_component_disk_usage()."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        """Reset the TTL cache before each test."""
        import dango.utils.db_health as mod

        mod._component_disk_cache = None
        mod._component_disk_cache_time = 0

    def _make_project(self, tmp_path: Path) -> Path:
        """Create a minimal project directory structure."""
        (tmp_path / "data" / "uploads" / "stripe").mkdir(parents=True)
        (tmp_path / "data" / "uploads" / "stripe" / "charges.csv").write_bytes(b"x" * (1024 * 1024))
        (tmp_path / "dbt" / "target").mkdir(parents=True)
        (tmp_path / "dbt" / "target" / "manifest.json").write_bytes(b"y" * (1024 * 1024))
        (tmp_path / ".dlt" / "pipelines").mkdir(parents=True)
        (tmp_path / ".dango" / "backups").mkdir(parents=True)
        return tmp_path

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_basic_breakdown(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        from dango.utils.db_health import get_component_disk_usage

        project = self._make_project(tmp_path)

        # No DuckDB file
        mock_run.return_value = MagicMock(returncode=1)  # Docker not available

        result = get_component_disk_usage(project)

        assert "duckdb" in result
        assert "metabase" in result
        assert "csv_uploads" in result
        assert "dbt_artifacts" in result
        assert "dlt_pipelines" in result
        assert "backups" in result
        assert "total_mb" in result

        # CSV uploads should have stripe source
        assert "stripe" in result["csv_uploads"]["by_source"]
        assert result["csv_uploads"]["total_mb"] > 0

        # dbt artifacts should be non-zero
        assert result["dbt_artifacts"]["size_mb"] > 0

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_duckdb_file_size(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        from dango.utils.db_health import get_component_disk_usage

        project = self._make_project(tmp_path)

        # Create a fake DuckDB file
        db_file = project / "data" / "warehouse.duckdb"
        db_file.write_bytes(b"\x00" * (1024 * 1024))  # 1 MB

        # Mock DuckDB connection for schema sizes
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("raw", 500000),
            ("staging", 300000),
        ]
        mock_duckdb.connect.return_value = mock_conn
        mock_run.return_value = MagicMock(returncode=1)

        result = get_component_disk_usage(project)

        assert result["duckdb"]["file_size_mb"] == 1.0
        assert result["duckdb"]["schema_sizes"] == {"raw": 500000, "staging": 300000}

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_metabase_size_from_docker(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        from dango.utils.db_health import get_component_disk_usage

        project = self._make_project(tmp_path)

        # Simulate successful docker stat
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="5242880\n",  # 5 MB in bytes
        )

        result = get_component_disk_usage(project)

        assert result["metabase"]["size_mb"] == 5.0

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_docker_not_available(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        from dango.utils.db_health import get_component_disk_usage

        project = self._make_project(tmp_path)
        mock_run.side_effect = FileNotFoundError("docker not found")

        result = get_component_disk_usage(project)

        assert result["metabase"]["size_mb"] is None

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_empty_project(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        """A project with no data directories still returns valid structure."""
        from dango.utils.db_health import get_component_disk_usage

        mock_run.return_value = MagicMock(returncode=1)

        result = get_component_disk_usage(tmp_path)

        assert result["duckdb"]["file_size_mb"] == 0
        assert result["csv_uploads"]["total_mb"] == 0
        assert result["total_mb"] == 0

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_cache_returns_same_result(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        """Second call within TTL returns cached result without recomputing."""
        from dango.utils.db_health import get_component_disk_usage

        project = self._make_project(tmp_path)
        mock_run.return_value = MagicMock(returncode=1)

        result1 = get_component_disk_usage(project)
        result2 = get_component_disk_usage(project)

        assert result1 is result2  # same object = from cache

    @patch("dango.utils.db_health.duckdb")
    @patch("dango.utils.db_health.subprocess.run")
    def test_cache_expires(
        self, mock_run: MagicMock, mock_duckdb: MagicMock, tmp_path: Path
    ) -> None:
        """After TTL expires, result is recomputed."""
        import dango.utils.db_health as mod
        from dango.utils.db_health import get_component_disk_usage

        project = self._make_project(tmp_path)
        mock_run.return_value = MagicMock(returncode=1)

        result1 = get_component_disk_usage(project)

        # Force cache expiry by backdating the cache time
        mod._component_disk_cache_time -= mod._COMPONENT_DISK_TTL + 1

        result2 = get_component_disk_usage(project)

        assert result1 is not result2  # different object = recomputed
