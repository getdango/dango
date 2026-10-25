"""tests/unit/test_serve_driver_integrity.py

``dango serve`` may continue past a driver-check failure only when the cached jar matches the
pinned digest (BUG-124 synced jar); a corrupt jar must stop before Docker services start.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.serve import serve
from tests.factories.driver_fakes import CORRUPT_JAR, GOOD_JAR, pin_driver, seed_jar
from tests.unit.test_serve_command import _CONFIG, _SERVE, _STARTUP, _UTILS, _make_config_mock


def _invoke(tmp_path, jar_bytes):
    seed_jar(tmp_path, jar_bytes, marker=None)
    with (
        patch("uvicorn.run") as uvicorn_run,
        patch(f"{_SERVE}._check_port"),
        patch(f"{_STARTUP}.import_dashboards"),
        patch(f"{_STARTUP}.setup_metabase_if_needed"),
        patch(f"{_STARTUP}.start_docker_services") as docker,
        patch(f"{_STARTUP}.ensure_duckdb_driver", side_effect=RuntimeError("network down")),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_CONFIG}.ConfigLoader", return_value=_make_config_mock()),
        patch(f"{_UTILS}.require_project_context", return_value=tmp_path),
    ):
        result = CliRunner().invoke(serve, [], obj={})
    return result, docker, uvicorn_run


@pytest.mark.unit
class TestServeDriverIntegrity:
    def test_serve_accepts_verified_synced_driver_offline(self, tmp_path, monkeypatch):
        pin_driver(monkeypatch)
        result, docker, uvicorn_run = _invoke(tmp_path, GOOD_JAR)
        assert result.exit_code == 0, result.output
        docker.assert_called_once()
        uvicorn_run.assert_called_once()
        assert "JAR exists" not in result.output

    def test_serve_rejects_corrupt_synced_driver(self, tmp_path, monkeypatch):
        pin_driver(monkeypatch)
        result, docker, uvicorn_run = _invoke(tmp_path, CORRUPT_JAR)
        assert result.exit_code != 0
        docker.assert_not_called()
        uvicorn_run.assert_not_called()
        assert "JAR exists" not in result.output
