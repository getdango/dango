"""tests/unit/test_driver_integrity.py

Pinned Metabase DuckDB driver integrity: cached jars and downloaded candidates must match
the pinned SHA-256/size before use; a failed fetch never replaces or leaves a bad jar.
"""

from __future__ import annotations

import pytest

from dango.platform.common.startup import ensure_duckdb_driver
from dango.utils import driver
from tests.factories.driver_fakes import (
    CORRUPT_JAR,
    GOOD_JAR,
    JAR_NAME,
    TRUNCATED_JAR,
    patch_network,
    pin_driver,
    plugin_files,
    seed_jar,
)

VERSION = driver.METABASE_DUCKDB_DRIVER_VERSION


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_driver(monkeypatch)


def _marker(plugins) -> str | None:
    return driver.read_driver_version(plugins)


@pytest.mark.unit
class TestDriverIntegrity:
    def test_verified_legacy_driver_repairs_marker_offline(self, tmp_path):
        plugins = seed_jar(tmp_path, GOOD_JAR, marker=None)
        with patch_network([OSError("offline")]) as (script, _sleep):
            ensure_duckdb_driver(tmp_path)
        assert script.calls == []
        assert _marker(plugins) == VERSION
        assert (plugins / JAR_NAME).read_bytes() == GOOD_JAR

    def test_corrupt_cached_driver_is_rejected(self, tmp_path):
        plugins = seed_jar(tmp_path, CORRUPT_JAR, marker=VERSION)
        assert driver.driver_needs_update(plugins) is True
        with patch_network([OSError("offline")]) as (_script, _sleep):
            with pytest.raises(RuntimeError):
                ensure_duckdb_driver(tmp_path)
        with patch_network([GOOD_JAR]):
            ensure_duckdb_driver(tmp_path)
        assert (plugins / JAR_NAME).read_bytes() == GOOD_JAR
        assert driver.driver_needs_update(plugins) is False

    def test_missing_driver_installs_verified_candidate(self, tmp_path):
        with patch_network([TRUNCATED_JAR, GOOD_JAR]) as (script, sleep):
            ensure_duckdb_driver(tmp_path)
        plugins = tmp_path / "metabase-plugins"
        assert len(script.calls) == 2
        sleep.assert_called_once_with(2)
        assert script.calls[0]["kwargs"].get("timeout") == 120
        assert (plugins / JAR_NAME).read_bytes() == GOOD_JAR
        assert _marker(plugins) == VERSION
        assert plugin_files(plugins) == [".driver-version", JAR_NAME]

    def test_bad_download_never_installs_driver(self, tmp_path):
        plugins = tmp_path / "metabase-plugins"
        with patch_network([CORRUPT_JAR]) as (script, _sleep):
            with pytest.raises(driver.DriverIntegrityError):
                ensure_duckdb_driver(tmp_path)
        assert len(script.calls) == 3
        assert plugin_files(plugins) == []
        with patch_network([OSError("down")]) as (script, _sleep):
            with pytest.raises(driver.DriverDownloadError, match="3 attempts"):
                ensure_duckdb_driver(tmp_path)
        assert len(script.calls) == 3
        assert plugin_files(plugins) == []
        # An over-long body is rejected too, not installed.
        with patch_network([GOOD_JAR + b"extra"]):
            with pytest.raises(driver.DriverIntegrityError):
                ensure_duckdb_driver(tmp_path)
        assert plugin_files(plugins) == []

    def test_failed_upgrade_preserves_old_jar_but_refuses_use(self, tmp_path):
        plugins = seed_jar(tmp_path, b"old driver bytes", marker="1.3.0")
        with patch_network([OSError("down")]):
            with pytest.raises(RuntimeError):
                ensure_duckdb_driver(tmp_path)
        assert (plugins / JAR_NAME).read_bytes() == b"old driver bytes"
        assert _marker(plugins) == "1.3.0"
        assert plugin_files(plugins) == [".driver-version", JAR_NAME]
        assert driver.verify_driver_jar(plugins / JAR_NAME) is False
        with patch_network([CORRUPT_JAR]):
            with pytest.raises(driver.DriverIntegrityError):
                ensure_duckdb_driver(tmp_path)
        assert (plugins / JAR_NAME).read_bytes() == b"old driver bytes"
        assert plugin_files(plugins) == [".driver-version", JAR_NAME]

    def test_progress_callback_and_missing_content_length(self, tmp_path):
        seen: list[tuple[int, int | None]] = []
        plugins = tmp_path / "metabase-plugins"
        plugins.mkdir()
        with patch_network([GOOD_JAR]):
            driver.download_verified_driver(plugins, lambda done, total: seen.append((done, total)))
        assert seen and seen[-1] == (len(GOOD_JAR), len(GOOD_JAR))

    def test_verify_rejects_missing_path_and_directory(self, tmp_path):
        assert driver.verify_driver_jar(tmp_path / "nope.jar") is False
        assert driver.verify_driver_jar(tmp_path) is False


@pytest.mark.unit
class TestRedirectPolicy:
    """The download opener refuses any redirect that leaves HTTPS (L1)."""

    @staticmethod
    def _redirect(url):
        import urllib.request

        opener = driver._build_opener()
        handler = next(
            h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
        )
        request = urllib.request.Request(driver.get_duckdb_driver_url())
        return handler.redirect_request(request, None, 302, "Found", {}, url)

    @pytest.mark.parametrize(
        "target", ["http://example.com/x.jar", "ftp://example.com/x.jar", "file:///etc/passwd"]
    )
    def test_non_https_redirect_is_refused(self, target):
        with pytest.raises(driver.DriverDownloadError):
            self._redirect(target)

    def test_https_redirect_is_followed(self):
        assert self._redirect("https://release-assets.githubusercontent.com/x.jar") is not None

    def test_refused_redirect_counts_as_failed_attempt(self, tmp_path):
        refusal = driver.DriverDownloadError("redirect refused")
        with patch_network([refusal]) as (script, _sleep):
            with pytest.raises(driver.DriverDownloadError, match="3 attempts"):
                ensure_duckdb_driver(tmp_path)
        assert len(script.calls) == 3
        assert plugin_files(tmp_path / "metabase-plugins") == []


@pytest.mark.unit
class TestLocalFilesystemFailures:
    """Local write failures become a clean, path-free DriverFilesystemError (L3)."""

    def test_mkstemp_failure_is_wrapped(self, tmp_path, monkeypatch):
        import tempfile

        def _deny(*_a, **_k):
            raise PermissionError(13, "denied", str(tmp_path / "secret"))

        monkeypatch.setattr(tempfile, "mkstemp", _deny)
        with patch_network([GOOD_JAR]) as (script, _sleep):
            with pytest.raises(driver.DriverFilesystemError) as info:
                ensure_duckdb_driver(tmp_path)
        assert script.calls == []
        assert str(tmp_path) not in str(info.value)
        assert info.value.__cause__ is None

    def test_marker_write_failure_is_wrapped(self, tmp_path, monkeypatch):
        def _deny(*_a, **_k):
            raise PermissionError(13, "denied", str(tmp_path / "secret"))

        monkeypatch.setattr(driver, "write_driver_version", _deny)
        with patch_network([GOOD_JAR]) as (script, _sleep):
            with pytest.raises(driver.DriverFilesystemError) as info:
                ensure_duckdb_driver(tmp_path)
        assert len(script.calls) == 1
        assert str(tmp_path) not in str(info.value)

    def test_marker_repair_failure_is_wrapped(self, tmp_path, monkeypatch):
        seed_jar(tmp_path, GOOD_JAR, marker=None)

        def _deny(*_a, **_k):
            raise PermissionError(13, "denied")

        monkeypatch.setattr(driver, "write_driver_version", _deny)
        with pytest.raises(driver.DriverFilesystemError):
            ensure_duckdb_driver(tmp_path)

    def test_filesystem_error_is_a_runtime_error_not_integrity(self):
        assert issubclass(driver.DriverFilesystemError, RuntimeError)
        assert not issubclass(driver.DriverFilesystemError, driver.DriverIntegrityError)
