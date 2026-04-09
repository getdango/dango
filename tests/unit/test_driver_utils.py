"""tests/unit/test_driver_utils.py

Tests for dango.utils.driver — Metabase DuckDB driver version management.
"""

from unittest.mock import patch

import pytest

from dango.utils.driver import (
    driver_needs_update,
    get_duckdb_driver_url,
    read_driver_version,
    write_driver_version,
)


@pytest.mark.unit
class TestGetDuckdbDriverUrl:
    def test_url_contains_version(self):
        """URL includes duckdb version with .0 suffix."""
        with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
            url = get_duckdb_driver_url()

        assert "/1.4.4.0/" in url
        assert url.endswith("duckdb.metabase-driver.jar")

    def test_url_format_different_version(self):
        """URL adapts to whatever version is installed."""
        with patch("dango.utils.driver.get_duckdb_version", return_value="1.5.1"):
            url = get_duckdb_driver_url()

        assert "/1.5.1.0/" in url


@pytest.mark.unit
class TestDriverVersionTracking:
    def test_read_missing_file(self, tmp_path):
        """read_driver_version returns None when file does not exist."""
        assert read_driver_version(tmp_path) is None

    def test_write_then_read(self, tmp_path):
        """Round-trip: write a version, read it back."""
        write_driver_version(tmp_path, "1.4.4")
        assert read_driver_version(tmp_path) == "1.4.4"

    def test_read_empty_file(self, tmp_path):
        """read_driver_version returns None for an empty file."""
        (tmp_path / ".driver-version").write_text("")
        assert read_driver_version(tmp_path) is None


@pytest.mark.unit
class TestDriverNeedsUpdate:
    def test_no_jar(self, tmp_path):
        """Needs update when driver jar does not exist."""
        with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
            assert driver_needs_update(tmp_path) is True

    def test_jar_exists_no_version_file(self, tmp_path):
        """Needs update when jar exists but version file is missing."""
        (tmp_path / "duckdb.metabase-driver.jar").touch()
        with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
            assert driver_needs_update(tmp_path) is True

    def test_version_mismatch(self, tmp_path):
        """Needs update when recorded version differs from installed."""
        (tmp_path / "duckdb.metabase-driver.jar").touch()
        write_driver_version(tmp_path, "1.3.0")
        with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
            assert driver_needs_update(tmp_path) is True

    def test_up_to_date(self, tmp_path):
        """No update needed when jar exists and version matches."""
        (tmp_path / "duckdb.metabase-driver.jar").touch()
        write_driver_version(tmp_path, "1.4.4")
        with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
            assert driver_needs_update(tmp_path) is False
