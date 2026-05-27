"""tests/unit/test_driver_utils.py

Tests for dango.utils.driver — Metabase DuckDB driver version management.
"""

from unittest.mock import patch

import pytest

from dango.exceptions import VersionMismatchError
from dango.utils.driver import (
    METABASE_DUCKDB_DRIVER_VERSION,
    check_version_alignment,
    driver_needs_update,
    get_duckdb_driver_url,
    read_driver_version,
    write_driver_version,
)


@pytest.mark.unit
class TestGetDuckdbDriverUrl:
    def test_url_contains_pinned_version(self):
        """URL includes the pinned Metabase driver version."""
        url = get_duckdb_driver_url()

        assert f"/{METABASE_DUCKDB_DRIVER_VERSION}/" in url
        assert url.endswith("duckdb.metabase-driver.jar")


@pytest.mark.unit
class TestDriverVersionTracking:
    def test_read_missing_file(self, tmp_path):
        """read_driver_version returns None when file does not exist."""
        assert read_driver_version(tmp_path) is None

    def test_write_then_read(self, tmp_path):
        """Round-trip: write a version, read it back."""
        write_driver_version(tmp_path, "1.5.1.0")
        assert read_driver_version(tmp_path) == "1.5.1.0"

    def test_read_empty_file(self, tmp_path):
        """read_driver_version returns None for an empty file."""
        (tmp_path / ".driver-version").write_text("")
        assert read_driver_version(tmp_path) is None


@pytest.mark.unit
class TestDriverNeedsUpdate:
    def test_no_jar(self, tmp_path):
        """Needs update when driver jar does not exist."""
        assert driver_needs_update(tmp_path) is True

    def test_jar_exists_no_version_file(self, tmp_path):
        """Needs update when jar exists but version file is missing."""
        (tmp_path / "duckdb.metabase-driver.jar").touch()
        assert driver_needs_update(tmp_path) is True

    def test_version_mismatch(self, tmp_path):
        """Needs update when recorded version differs from pinned."""
        (tmp_path / "duckdb.metabase-driver.jar").touch()
        write_driver_version(tmp_path, "1.3.0")
        assert driver_needs_update(tmp_path) is True

    def test_up_to_date(self, tmp_path):
        """No update needed when jar exists and version matches pinned."""
        (tmp_path / "duckdb.metabase-driver.jar").touch()
        write_driver_version(tmp_path, METABASE_DUCKDB_DRIVER_VERSION)
        assert driver_needs_update(tmp_path) is False


@pytest.mark.unit
class TestCheckVersionAlignment:
    def test_matching_versions_no_error(self):
        """No exception when Python DuckDB and driver share major.minor."""
        with patch("duckdb.__version__", "1.5.2"):
            check_version_alignment()  # Should not raise

    def test_mismatched_versions_raises(self):
        """VersionMismatchError raised when major.minor differs."""
        with patch("duckdb.__version__", "1.4.4"):
            with pytest.raises(VersionMismatchError, match="does not match") as exc_info:
                check_version_alignment()
            assert exc_info.value.error_code == "DANGO-U008"

    def test_three_vs_four_segment_match(self):
        """Three-segment Python version matches four-segment driver version."""
        # Python: "1.5.0" → major.minor "1.5"
        # Driver: "1.5.1.0" → major.minor "1.5"
        with patch("duckdb.__version__", "1.5.0"):
            check_version_alignment()  # Should not raise
