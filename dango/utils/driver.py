"""dango/utils/driver.py

Metabase DuckDB driver version management.

Provides pinned driver URL and version tracking to detect when the
driver needs re-downloading.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path

DRIVER_VERSION_FILE = ".driver-version"
"""Filename written to ``metabase-plugins/`` after a successful driver download."""

# Rule: Python DuckDB major.minor MUST match driver's bundled DuckDB major.minor.
# DuckDB 1.5.x read-only mode CANNOT read files created by 1.4.x.
# Current alignment: Python DuckDB 1.5.x, driver 1.5.4.0, Metabase v0.62.18
#
# When upgrading: check https://github.com/motherduckdb/metabase_duckdb_driver/releases
# for the driver that targets your Metabase version. The driver's bundled DuckDB must
# share the same major.minor as the Python DuckDB version.
METABASE_DUCKDB_DRIVER_VERSION = "1.5.4.0"

METABASE_DUCKDB_DRIVER_URL = (
    "https://github.com/motherduckdb/metabase_duckdb_driver/"
    f"releases/download/{METABASE_DUCKDB_DRIVER_VERSION}/duckdb.metabase-driver.jar"
)

# SHA-256 and byte size of the 1.5.4.0 release asset, taken from the publisher's GitHub
# release-asset metadata (asset id 471374348). These must be updated together with
# METABASE_DUCKDB_DRIVER_VERSION and are never derived from the downloaded bytes.
METABASE_DUCKDB_DRIVER_SHA256 = "cc73f4d726b68b15e1a89974feed93348b1bfbfa0646f0d9e9c64ce6aa8ee7ba"
METABASE_DUCKDB_DRIVER_SIZE = 84156889

DRIVER_JAR_NAME = "duckdb.metabase-driver.jar"
_DOWNLOAD_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 2
_REQUEST_TIMEOUT_SECONDS = 120
_CHUNK_SIZE = 65536


class DriverIntegrityError(RuntimeError):
    """Driver bytes do not match the pinned size/SHA-256."""


class DriverDownloadError(RuntimeError):
    """Driver could not be fetched (network or read failure) after bounded attempts."""


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


def verify_driver_jar(path: Path) -> bool:
    """Return ``True`` only for a regular file with the pinned size and SHA-256."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return False
        if path.stat().st_size != METABASE_DUCKDB_DRIVER_SIZE:
            return False
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                digest.update(chunk)
    except OSError:
        return False
    return digest.hexdigest() == METABASE_DUCKDB_DRIVER_SHA256


def driver_needs_update(plugins_dir: Path) -> bool:
    """Return ``True`` unless the jar matches the pinned digest and the marker matches.

    A version marker alone is never proof: the jar bytes must verify.
    """
    if not verify_driver_jar(plugins_dir / DRIVER_JAR_NAME):
        return True
    return read_driver_version(plugins_dir) != METABASE_DUCKDB_DRIVER_VERSION


def reuse_verified_driver(plugins_dir: Path) -> bool:
    """Return ``True`` if a verified jar is installed, repairing a missing/stale marker."""
    if not verify_driver_jar(plugins_dir / DRIVER_JAR_NAME):
        return False
    if read_driver_version(plugins_dir) != METABASE_DUCKDB_DRIVER_VERSION:
        write_driver_version(plugins_dir, METABASE_DUCKDB_DRIVER_VERSION)
    return True


def _content_length(header: str | None) -> int | None:
    """Parse Content-Length for progress display only; invalid or absent returns ``None``."""
    try:
        value = int(header) if header is not None else 0
    except ValueError:
        return None
    return value if value > 0 else None


def _fetch_candidate(tmp_path: Path, progress: Callable[[int, int | None], None] | None) -> None:
    """Stream the pinned URL into *tmp_path*, then check size and SHA-256."""
    digest = hashlib.sha256()
    downloaded = 0
    # HTTPS-only opener (default redirect handling): the pinned URL can never resolve to file:/ or ftp:.
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler())
    with opener.open(get_duckdb_driver_url(), timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        length = _content_length(response.headers.get("Content-Length"))
        with open(tmp_path, "wb") as out:
            while chunk := response.read(_CHUNK_SIZE):
                downloaded += len(chunk)
                if downloaded > METABASE_DUCKDB_DRIVER_SIZE:
                    raise DriverIntegrityError("Downloaded DuckDB driver is larger than expected.")
                digest.update(chunk)
                out.write(chunk)
                if progress is not None:
                    progress(downloaded, length)
    if (
        downloaded != METABASE_DUCKDB_DRIVER_SIZE
        or digest.hexdigest() != METABASE_DUCKDB_DRIVER_SHA256
    ):
        raise DriverIntegrityError("Downloaded DuckDB driver does not match the pinned checksum.")


def download_verified_driver(
    plugins_dir: Path, progress: Callable[[int, int | None], None] | None = None
) -> None:
    """Download the pinned driver, verify it, and install it atomically with its marker.

    Up to three attempts, two seconds apart. Nothing is placed at the production path
    unless the candidate matches the pinned size and SHA-256; an existing jar and marker
    are left untouched on failure.

    Raises:
        DriverIntegrityError: The last failed attempt returned wrong bytes.
        DriverDownloadError: The last failed attempt was a network/read failure.
    """
    plugins_dir.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        if attempt > 0:
            time.sleep(_RETRY_DELAY_SECONDS)
        fd, tmp_name = tempfile.mkstemp(dir=plugins_dir, prefix=".driver-", suffix=".tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            _fetch_candidate(tmp_path, progress)
            tmp_path.chmod(0o644)
            os.replace(tmp_path, plugins_dir / DRIVER_JAR_NAME)
            write_driver_version(plugins_dir, METABASE_DUCKDB_DRIVER_VERSION)
            return
        except Exception as exc:  # integrity mismatch, or network/TLS/read/disk failure
            last_error = exc
        finally:
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    if isinstance(last_error, DriverIntegrityError):
        raise DriverIntegrityError(
            "DuckDB driver integrity verification failed: the downloaded file did not match "
            f"the pinned checksum after {_DOWNLOAD_ATTEMPTS} attempts."
        ) from None
    raise DriverDownloadError(
        f"Failed to download DuckDB driver after {_DOWNLOAD_ATTEMPTS} attempts. "
        f"URL: {get_duckdb_driver_url()} - Check your internet connection and try again, "
        "or download manually from: "
        "https://github.com/motherduckdb/metabase_duckdb_driver/releases"
    ) from None


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
