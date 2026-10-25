"""tests/unit/test_init_driver_integrity.py

``dango init`` driver setup: reuse a verified jar offline, and report a digest mismatch as an
integrity failure (not a network issue) without installing the bad candidate.
"""

from __future__ import annotations

import pytest

from dango.cli.init import ProjectInitializer
from dango.utils import driver
from tests.factories.driver_fakes import (
    CORRUPT_JAR,
    GOOD_JAR,
    JAR_NAME,
    patch_network,
    pin_driver,
    plugin_files,
    seed_jar,
)


@pytest.fixture(autouse=True)
def _pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    pin_driver(monkeypatch)


@pytest.mark.unit
class TestInitDriverIntegrity:
    def test_init_digest_mismatch_reports_integrity_failure(self, tmp_path, capsys):
        with patch_network([CORRUPT_JAR]):
            ok = ProjectInitializer(tmp_path)._setup_metabase()
        out = capsys.readouterr().out
        assert ok is False
        assert "DuckDB driver integrity verification failed" in out
        assert "network issue" not in out
        assert "dango start" in out
        assert plugin_files(tmp_path / "metabase-plugins") == []

    def test_init_network_failure_keeps_network_message(self, tmp_path, capsys):
        with patch_network([OSError("down")]):
            ok = ProjectInitializer(tmp_path)._setup_metabase()
        out = capsys.readouterr().out
        assert ok is False
        assert "network issue" in out
        assert "integrity" not in out

    def test_init_installs_verified_driver_and_reuses_it_offline(self, tmp_path):
        with patch_network([GOOD_JAR]) as (script, _sleep):
            assert ProjectInitializer(tmp_path)._setup_metabase() is True
        assert len(script.calls) == 1
        plugins = tmp_path / "metabase-plugins"
        assert (plugins / JAR_NAME).read_bytes() == GOOD_JAR
        (plugins / ".driver-version").unlink()
        with patch_network([OSError("offline")]) as (script, _sleep):
            assert ProjectInitializer(tmp_path)._setup_metabase() is True
        assert script.calls == []
        assert driver.read_driver_version(plugins) == driver.METABASE_DUCKDB_DRIVER_VERSION

    def test_init_refuses_corrupt_preseeded_driver(self, tmp_path):
        seed_jar(tmp_path, CORRUPT_JAR, marker=driver.METABASE_DUCKDB_DRIVER_VERSION)
        with patch_network([GOOD_JAR]) as (script, _sleep):
            assert ProjectInitializer(tmp_path)._setup_metabase() is True
        assert len(script.calls) == 1
        assert (tmp_path / "metabase-plugins" / JAR_NAME).read_bytes() == GOOD_JAR
