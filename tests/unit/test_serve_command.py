"""tests/unit/test_serve_command.py

Unit tests for the ``dango serve`` CLI command
(dango/cli/commands/serve.py).

All startup helpers are mocked — no Docker, Metabase, or network calls.
Patches target origin modules because serve.py uses lazy imports.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from dango.cli.commands.serve import serve

# Patch targets — lazy imports mean we patch at origin, not at consumption site.
_UTILS = "dango.cli.utils"
_CONFIG = "dango.config"
_STARTUP = "dango.platform.common.startup"
_SERVE = "dango.cli.commands.serve"


def _make_config_mock(port: int = 8800) -> MagicMock:
    """Return a mock ConfigLoader whose load_config() returns a config with the given port."""
    config = MagicMock()
    config.project.name = "test-project"
    config.project.organization = None
    config.platform.port = port
    loader = MagicMock()
    loader.load_config.return_value = config
    return loader


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServeHappyPath:
    @patch("uvicorn.run")
    @patch(f"{_SERVE}._check_port")
    @patch(f"{_STARTUP}.import_dashboards")
    @patch(f"{_STARTUP}.setup_metabase_if_needed")
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_all_helpers_called(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        mock_dashboards,
        mock_check_port,
        mock_uvicorn_run,
        tmp_path,
    ):
        """All startup helpers are called in order, uvicorn runs."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock(port=8800)

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 0, result.output
        mock_migrate.assert_called_once_with(tmp_path)
        mock_schemas.assert_called_once_with(tmp_path)
        mock_driver.assert_called_once_with(tmp_path)
        mock_docker.assert_called_once_with(tmp_path)
        mock_metabase.assert_called_once_with(tmp_path, "test-project", None)
        mock_dashboards.assert_called_once_with(tmp_path)
        mock_check_port.assert_called_once_with(8800)
        mock_uvicorn_run.assert_called_once_with(
            "dango.web.app:app", host="0.0.0.0", port=8800, log_level="info"
        )

    @patch("uvicorn.run")
    @patch(f"{_SERVE}._check_port")
    @patch(f"{_STARTUP}.import_dashboards")
    @patch(f"{_STARTUP}.setup_metabase_if_needed")
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_cli_port_overrides_config(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        mock_dashboards,
        mock_check_port,
        mock_uvicorn_run,
        tmp_path,
    ):
        """--port CLI flag overrides config port."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock(port=8800)

        runner = CliRunner()
        result = runner.invoke(serve, ["--port", "9000"], obj={})

        assert result.exit_code == 0, result.output
        mock_check_port.assert_called_once_with(9000)
        mock_uvicorn_run.assert_called_once_with(
            "dango.web.app:app", host="0.0.0.0", port=9000, log_level="info"
        )

    @patch("uvicorn.run")
    @patch(f"{_SERVE}._check_port")
    @patch(f"{_STARTUP}.import_dashboards")
    @patch(f"{_STARTUP}.setup_metabase_if_needed")
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_host_option(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        mock_dashboards,
        mock_check_port,
        mock_uvicorn_run,
        tmp_path,
    ):
        """--host CLI flag is passed to uvicorn."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        runner = CliRunner()
        result = runner.invoke(serve, ["--host", "127.0.0.1"], obj={})

        assert result.exit_code == 0, result.output
        mock_uvicorn_run.assert_called_once_with(
            "dango.web.app:app", host="127.0.0.1", port=8800, log_level="info"
        )


# ---------------------------------------------------------------------------
# 2. Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServeFailures:
    @patch(f"{_UTILS}.require_project_context", side_effect=click.Abort())
    def test_project_context_abort_exits(self, mock_ctx):
        """require_project_context abort causes SystemExit(1) (L4)."""
        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 1

    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_config_load_failure_exits(self, mock_ctx, mock_loader_cls, tmp_path):
        """Config load failure causes clean exit (M6)."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.side_effect = RuntimeError("bad config")

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 1
        assert "Failed to load project config" in result.output

    @patch(f"{_STARTUP}.run_pending_migrations", side_effect=RuntimeError("migration err"))
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_migration_failure_exits(self, mock_ctx, mock_loader_cls, mock_migrate, tmp_path):
        """Migration failure causes SystemExit(1)."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 1
        assert "Migration failed" in result.output

    @patch(f"{_STARTUP}.ensure_dbt_schemas", side_effect=RuntimeError("schema err"))
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_schema_failure_exits(
        self, mock_ctx, mock_loader_cls, mock_migrate, mock_schemas, tmp_path
    ):
        """Schema setup failure causes SystemExit(1) (L4)."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 1
        assert "Schema setup failed" in result.output

    @patch(f"{_STARTUP}.ensure_duckdb_driver", side_effect=RuntimeError("driver err"))
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_duckdb_driver_failure_exits(
        self, mock_ctx, mock_loader_cls, mock_migrate, mock_schemas, mock_driver, tmp_path
    ):
        """DuckDB driver failure causes SystemExit(1) (L4)."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 1
        assert "DuckDB driver download failed" in result.output

    @patch(f"{_STARTUP}.start_docker_services", side_effect=RuntimeError("docker err"))
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_docker_failure_stops_and_exits(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        tmp_path,
    ):
        """Docker failure stops Docker services and exits."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        with patch(f"{_SERVE}._stop_docker_quiet") as mock_stop:
            runner = CliRunner()
            result = runner.invoke(serve, [], obj={})

            assert result.exit_code == 1
            assert "Docker services failed" in result.output
            mock_stop.assert_called_once()

    @patch(f"{_STARTUP}.setup_metabase_if_needed", side_effect=RuntimeError("metabase err"))
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_metabase_failure_stops_and_exits(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        tmp_path,
    ):
        """Metabase failure stops Docker services and exits."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        with patch(f"{_SERVE}._stop_docker_quiet") as mock_stop:
            runner = CliRunner()
            result = runner.invoke(serve, [], obj={})

            assert result.exit_code == 1
            assert "Metabase setup failed" in result.output
            mock_stop.assert_called_once()

    @patch("uvicorn.run")
    @patch(f"{_SERVE}._check_port")
    @patch(f"{_STARTUP}.import_dashboards", side_effect=RuntimeError("dashboard err"))
    @patch(f"{_STARTUP}.setup_metabase_if_needed")
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_dashboard_failure_non_critical(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        mock_dashboards,
        mock_check_port,
        mock_uvicorn_run,
        tmp_path,
    ):
        """Dashboard import failure is swallowed — server still starts."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 0, result.output
        mock_uvicorn_run.assert_called_once()

    @patch(f"{_SERVE}._check_port", side_effect=SystemExit(1))
    @patch(f"{_STARTUP}.import_dashboards")
    @patch(f"{_STARTUP}.setup_metabase_if_needed")
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_port_in_use_exits(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        mock_dashboards,
        mock_check_port,
        tmp_path,
    ):
        """Port in use causes clean exit (H4)."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        runner = CliRunner()
        result = runner.invoke(serve, [], obj={})

        assert result.exit_code == 1

    @patch("uvicorn.run", side_effect=RuntimeError("bind failed"))
    @patch(f"{_SERVE}._check_port")
    @patch(f"{_STARTUP}.import_dashboards")
    @patch(f"{_STARTUP}.setup_metabase_if_needed")
    @patch(f"{_STARTUP}.start_docker_services")
    @patch(f"{_STARTUP}.ensure_duckdb_driver")
    @patch(f"{_STARTUP}.ensure_dbt_schemas")
    @patch(f"{_STARTUP}.run_pending_migrations", return_value={})
    @patch(f"{_CONFIG}.ConfigLoader")
    @patch(f"{_UTILS}.require_project_context")
    def test_uvicorn_failure_stops_docker(
        self,
        mock_ctx,
        mock_loader_cls,
        mock_migrate,
        mock_schemas,
        mock_driver,
        mock_docker,
        mock_metabase,
        mock_dashboards,
        mock_check_port,
        mock_uvicorn_run,
        tmp_path,
    ):
        """uvicorn failure triggers Docker cleanup (H3)."""
        mock_ctx.return_value = tmp_path
        mock_loader_cls.return_value = _make_config_mock()

        with patch(f"{_SERVE}._stop_docker_quiet") as mock_stop:
            runner = CliRunner()
            result = runner.invoke(serve, [], obj={})

            assert result.exit_code != 0
            mock_stop.assert_called_once()
