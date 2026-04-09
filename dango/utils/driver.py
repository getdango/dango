"""dango/utils/driver.py

Metabase DuckDB driver version management.

Provides dynamic driver URL generation based on the installed DuckDB version
and version tracking to detect when the driver needs re-downloading.
"""

from __future__ import annotations

from pathlib import Path

DRIVER_VERSION_FILE = ".driver-version"
"""Filename written to ``metabase-plugins/`` after a successful driver download."""

# NOTE: Always appends ".0" to the DuckDB version. The MotherDuck repo also has
# patch releases (e.g. 1.4.1.1, 1.4.3.1) but we pin DuckDB to an exact version
# whose .0 driver is verified to exist. When bumping DuckDB, verify the .0
# release exists at https://github.com/motherduckdb/metabase_duckdb_driver/releases
_DRIVER_URL_TEMPLATE = (
    "https://github.com/motherduckdb/metabase_duckdb_driver/"
    "releases/download/{version}.0/duckdb.metabase-driver.jar"
)


def get_duckdb_version() -> str:
    """Return the installed DuckDB version string (e.g. ``"1.4.4"``)."""
    import duckdb

    return duckdb.__version__


def get_duckdb_driver_url() -> str:
    """Build the Metabase DuckDB driver download URL for the installed version.

    The MotherDuck driver releases use a four-part version scheme
    (e.g. ``1.4.4.0``) where the first three parts match ``duckdb.__version__``.
    """
    return _DRIVER_URL_TEMPLATE.format(version=get_duckdb_version())


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
    return recorded != get_duckdb_version()
