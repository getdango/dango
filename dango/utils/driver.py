"""dango/utils/driver.py

Metabase DuckDB driver version management.

Provides pinned driver URL and version tracking to detect when the
driver needs re-downloading.
"""

from __future__ import annotations

from pathlib import Path

DRIVER_VERSION_FILE = ".driver-version"
"""Filename written to ``metabase-plugins/`` after a successful driver download."""

# Rule: Python DuckDB major.minor MUST match driver's bundled DuckDB major.minor.
# DuckDB 1.5.x read-only mode CANNOT read files created by 1.4.x.
# Current alignment: Python DuckDB 1.5.x, driver 1.5.1.0, Metabase v0.59.1
#
# When upgrading: check https://github.com/motherduckdb/metabase_duckdb_driver/releases
# for the driver that targets your Metabase version. The driver's bundled DuckDB must
# share the same major.minor as the Python DuckDB version.
METABASE_DUCKDB_DRIVER_VERSION = "1.5.1.0"

METABASE_DUCKDB_DRIVER_URL = (
    "https://github.com/motherduckdb/metabase_duckdb_driver/"
    f"releases/download/{METABASE_DUCKDB_DRIVER_VERSION}/duckdb.metabase-driver.jar"
)


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
    3. Recorded version differs from ``METABASE_DUCKDB_DRIVER_VERSION`` → needs update.
    """
    driver_jar = plugins_dir / "duckdb.metabase-driver.jar"
    if not driver_jar.exists():
        return True
    recorded = read_driver_version(plugins_dir)
    if recorded is None:
        return True
    return recorded != METABASE_DUCKDB_DRIVER_VERSION


def check_version_alignment() -> None:
    """Verify Python DuckDB and Metabase JDBC driver share the same major.minor.

    Raises:
        VersionMismatchError: If major.minor versions differ.
    """
    import duckdb

    from dango.exceptions import VersionMismatchError

    python_version = duckdb.__version__
    driver_version = METABASE_DUCKDB_DRIVER_VERSION

    python_major_minor = ".".join(python_version.split(".")[:2])
    driver_major_minor = ".".join(driver_version.split(".")[:2])

    if python_major_minor != driver_major_minor:
        raise VersionMismatchError(
            f"DuckDB Python {python_version} (major.minor {python_major_minor}) "
            f"does not match Metabase JDBC driver {driver_version} "
            f"(major.minor {driver_major_minor}). They must match — DuckDB "
            f"read-only mode cannot cross major.minor versions. Update duckdb "
            f"in pyproject.toml or METABASE_DUCKDB_DRIVER_VERSION in driver.py."
        )
