"""dango/utils/driver.py

Metabase DuckDB driver version management.

Provides dynamic driver URL generation based on the installed DuckDB version
and version tracking to detect when the driver needs re-downloading.
"""

from __future__ import annotations

from pathlib import Path

DRIVER_VERSION_FILE = ".driver-version"
"""Filename written to ``metabase-plugins/`` after a successful driver download."""

# Driver version must match METABASE version, not DuckDB Python version.
# DuckDB 1.5.x can read files created by DuckDB 1.4.x (backwards compatible).
# Current alignment: Metabase v0.59.1 → driver 1.5.1.0 → reads DuckDB 1.4.4 files
#
# When upgrading: check https://github.com/motherduckdb/metabase_duckdb_driver/releases
# for the driver that targets your Metabase version. The driver's bundled DuckDB must
# be >= the Python DuckDB version for backwards-compatible reads.
METABASE_DUCKDB_DRIVER_VERSION = "1.5.1.0"

METABASE_DUCKDB_DRIVER_URL = (
    "https://github.com/motherduckdb/metabase_duckdb_driver/"
    f"releases/download/{METABASE_DUCKDB_DRIVER_VERSION}/duckdb.metabase-driver.jar"
)


def get_duckdb_version() -> str:
    """Return the installed DuckDB version string (e.g. ``"1.4.4"``)."""
    import duckdb

    return duckdb.__version__


def get_duckdb_driver_url() -> str:
    """Return the pinned Metabase DuckDB driver download URL."""
    return METABASE_DUCKDB_DRIVER_URL


def read_driver_version(plugins_dir: Path) -> str | None:
    """Read the previously downloaded driver version from *plugins_dir*.

    Returns ``None`` if the version file does not exist or cannot be read.
    """
    version_file = plugins_dir / DRIVER_VERSION_FILE
    try:
        return version_file.read_text().strip() or None
    except OSError:
        return None


def write_driver_version(plugins_dir: Path, version: str) -> None:
    """Write *version* to the driver version tracking file in *plugins_dir*."""
    version_file = plugins_dir / DRIVER_VERSION_FILE
    version_file.write_text(version + "\n")


def driver_needs_update(plugins_dir: Path) -> bool:
    """Return ``True`` if the driver jar is missing or its version mismatches.

    Checks:
    1. Driver jar file does not exist → needs update.
    2. Version tracking file is missing → needs update (legacy install).
    3. Recorded version differs from ``duckdb.__version__`` → needs update.
    """
    driver_jar = plugins_dir / "duckdb.metabase-driver.jar"
    if not driver_jar.exists():
        return True
    recorded = read_driver_version(plugins_dir)
    if recorded is None:
        return True
    return recorded != METABASE_DUCKDB_DRIVER_VERSION
