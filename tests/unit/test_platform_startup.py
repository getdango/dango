"""tests/unit/test_platform_startup.py

Tests for dango.platform.common.startup shared startup helpers.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.common.startup import (
    ensure_dbt_schemas,
    ensure_duckdb_driver,
    ensure_icu_extension,
    import_dashboards,
    run_pending_migrations,
    setup_metabase_if_needed,
    start_docker_services,
)


@pytest.mark.unit
class TestRunPendingMigrations:
    def test_returns_empty_dict_when_no_migrations(self, tmp_path):
        """run_pending_migrations returns empty dict when nothing applied."""
        with patch("dango.migrations.apply_all_pending", return_value={}) as mock_apply:
            result = run_pending_migrations(tmp_path)
        assert result == {}
        mock_apply.assert_called_once_with(tmp_path)

    def test_returns_applied_migrations(self, tmp_path):
        """run_pending_migrations passes through apply_all_pending return value."""
        applied = {"auth": ["001_create_users", "002_create_sessions"]}
        with patch("dango.migrations.apply_all_pending", return_value=applied):
            result = run_pending_migrations(tmp_path)
        assert result == applied


@pytest.mark.unit
class TestEnsureDbtSchemas:
    def test_calls_ensure_dbt_schemas_with_duckdb_path(self, tmp_path):
        """ensure_dbt_schemas calls the underlying helper with the warehouse path."""
        with patch("dango.utils.database.ensure_dbt_schemas") as mock_ensure:
            ensure_dbt_schemas(tmp_path)
        expected_path = tmp_path / "data" / "warehouse.duckdb"
        mock_ensure.assert_called_once_with(expected_path)


@pytest.mark.unit
class TestEnsureDuckdbDriver:
    def test_no_op_if_driver_exists_and_version_matches(self, tmp_path):
        """ensure_duckdb_driver skips download when jar and version match."""
        plugins_dir = tmp_path / "metabase-plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "duckdb.metabase-driver.jar").touch()
        (plugins_dir / ".driver-version").write_text("1.4.4\n")

        with patch("urllib.request.urlretrieve") as mock_retrieve:
            with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
                ensure_duckdb_driver(tmp_path)

        mock_retrieve.assert_not_called()

    def test_downloads_driver_if_missing(self, tmp_path):
        """ensure_duckdb_driver calls urlretrieve when driver is absent."""

        def fake_retrieve(url: str, dest: Path) -> None:
            """Simulate download by creating the destination file."""
            Path(dest).touch()

        with patch("urllib.request.urlretrieve", side_effect=fake_retrieve):
            with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
                ensure_duckdb_driver(tmp_path)

        plugins_dir = tmp_path / "metabase-plugins"
        driver_path = plugins_dir / "duckdb.metabase-driver.jar"
        assert driver_path.exists()
        assert (plugins_dir / ".driver-version").read_text().strip() == "1.4.4"

    def test_raises_runtime_error_after_3_failures(self, tmp_path):
        """ensure_duckdb_driver raises RuntimeError when all 3 attempts fail."""
        with patch("urllib.request.urlretrieve", side_effect=OSError("network error")):
            with patch("time.sleep"):  # Skip retry delays
                with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
                    with pytest.raises(RuntimeError, match="3 attempts"):
                        ensure_duckdb_driver(tmp_path)

    def test_retries_on_failure(self, tmp_path):
        """ensure_duckdb_driver retries up to 3 times before giving up."""
        call_count = 0

        def failing_retrieve(url: str, dest: Path) -> None:
            """Always raises to simulate persistent network failure."""
            nonlocal call_count
            call_count += 1
            raise OSError("network error")

        with patch("urllib.request.urlretrieve", side_effect=failing_retrieve):
            with patch("time.sleep"):
                with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
                    with pytest.raises(RuntimeError):
                        ensure_duckdb_driver(tmp_path)

        assert call_count == 3

    def test_redownloads_on_version_mismatch(self, tmp_path):
        """ensure_duckdb_driver re-downloads when version file doesn't match."""
        plugins_dir = tmp_path / "metabase-plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "duckdb.metabase-driver.jar").touch()
        (plugins_dir / ".driver-version").write_text("1.3.0\n")

        def fake_retrieve(url: str, dest: Path) -> None:
            Path(dest).touch()

        with patch("urllib.request.urlretrieve", side_effect=fake_retrieve) as mock_ret:
            with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
                ensure_duckdb_driver(tmp_path)

        mock_ret.assert_called_once()
        assert (plugins_dir / ".driver-version").read_text().strip() == "1.4.4"

    def test_redownloads_when_version_file_missing(self, tmp_path):
        """ensure_duckdb_driver re-downloads when version file is absent."""
        plugins_dir = tmp_path / "metabase-plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "duckdb.metabase-driver.jar").touch()

        def fake_retrieve(url: str, dest: Path) -> None:
            Path(dest).touch()

        with patch("urllib.request.urlretrieve", side_effect=fake_retrieve) as mock_ret:
            with patch("dango.utils.driver.get_duckdb_version", return_value="1.4.4"):
                ensure_duckdb_driver(tmp_path)

        mock_ret.assert_called_once()
        assert (plugins_dir / ".driver-version").read_text().strip() == "1.4.4"


@pytest.mark.unit
class TestStartDockerServices:
    def _make_manager(self) -> MagicMock:
        """Create a fully-configured DockerManager mock that passes all checks."""
        manager = MagicMock()
        manager.is_docker_daemon_running.return_value = True
        manager.start_services.return_value = True
        return manager

    def test_raises_if_daemon_not_running(self, tmp_path):
        """start_docker_services raises RuntimeError when Docker daemon is absent."""
        manager = self._make_manager()
        manager.is_docker_daemon_running.return_value = False

        with patch("dango.platform.DockerManager", return_value=manager):
            with pytest.raises(RuntimeError, match="Docker daemon"):
                start_docker_services(tmp_path)

    def test_raises_if_ports_still_occupied_after_cleanup(self, tmp_path):
        """start_docker_services raises RuntimeError when ports remain occupied."""
        manager = self._make_manager()

        # Simulate port always occupied (connect_ex returns 0 = success = occupied)
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 0  # port occupied

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", return_value=mock_sock):
                with pytest.raises(RuntimeError, match="still in use"):
                    start_docker_services(tmp_path)

    def test_raises_if_start_services_fails(self, tmp_path):
        """start_docker_services raises RuntimeError when start_services returns False."""
        manager = self._make_manager()
        manager.start_services.return_value = False

        # Ports are free (connect_ex returns non-zero = connection refused = port free)
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", return_value=mock_sock):
                with pytest.raises(RuntimeError, match="failed to start"):
                    start_docker_services(tmp_path)

    def test_success(self, tmp_path):
        """start_docker_services completes without error when all checks pass."""
        manager = self._make_manager()

        # Ports are free
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", return_value=mock_sock):
                start_docker_services(tmp_path)  # Should not raise

        manager.start_services.assert_called_once()


@pytest.mark.unit
class TestSetupMetabaseIfNeeded:
    def test_returns_already_configured_when_credentials_exist(self, tmp_path):
        """setup_metabase_if_needed returns already_configured=True when file present."""
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "metabase.yml").touch()

        result = setup_metabase_if_needed(tmp_path, "MyProject", None)

        assert result["already_configured"] is True
        assert result["success"] is True

    def test_raises_when_duckdb_not_connected(self, tmp_path):
        """setup_metabase_if_needed raises RuntimeError when DuckDB cannot connect."""
        setup_result = {"success": False, "duckdb_connected": False}

        with patch("dango.visualization.metabase.setup_metabase", return_value=setup_result):
            with pytest.raises(RuntimeError, match="connect Metabase"):
                setup_metabase_if_needed(tmp_path, "MyProject", None)

    def test_success(self, tmp_path):
        """setup_metabase_if_needed returns result dict on successful first-run setup."""
        setup_result = {
            "success": True,
            "duckdb_connected": True,
            "collections_created": ["MyProject"],
            "errors": [],
        }

        with patch("dango.visualization.metabase.setup_metabase", return_value=setup_result):
            result = setup_metabase_if_needed(tmp_path, "MyProject", "Acme Corp")

        assert result["already_configured"] is False
        assert result["success"] is True
        assert result["duckdb_connected"] is True


@pytest.mark.unit
class TestImportDashboards:
    def test_returns_none_when_no_dashboards_dir(self, tmp_path):
        """import_dashboards returns None when dashboards/ directory is absent."""
        result = import_dashboards(tmp_path)
        assert result is None

    def test_returns_none_when_no_yml_files(self, tmp_path):
        """import_dashboards returns None when dashboards/ has no .yml files."""
        (tmp_path / "dashboards").mkdir()
        result = import_dashboards(tmp_path)
        assert result is None

    def test_calls_import_when_dashboards_exist(self, tmp_path):
        """import_dashboards delegates to dashboard_manager when .yml files found."""
        dashboards_dir = tmp_path / "dashboards"
        dashboards_dir.mkdir()
        (dashboards_dir / "overview.yml").touch()

        expected = {"imported": 1, "skipped": 0}
        with patch(
            "dango.visualization.dashboard_manager.import_dashboards", return_value=expected
        ) as mock_import:
            result = import_dashboards(tmp_path)

        assert result == expected
        mock_import.assert_called_once_with(tmp_path)


@pytest.mark.unit
class TestEnsureIcuExtension:
    def test_no_op_when_db_does_not_exist(self, tmp_path):
        """ensure_icu_extension does nothing when warehouse.duckdb is absent."""
        with patch("duckdb.connect") as mock_connect:
            ensure_icu_extension(tmp_path)
        mock_connect.assert_not_called()

    def test_installs_icu_when_db_exists(self, tmp_path):
        """ensure_icu_extension runs INSTALL/LOAD icu on existing warehouse."""
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        db_path.touch()

        mock_conn = MagicMock()
        with patch("duckdb.connect", return_value=mock_conn) as mock_connect:
            ensure_icu_extension(tmp_path)

        mock_connect.assert_called_once_with(str(db_path))
        assert mock_conn.execute.call_count == 2
        mock_conn.execute.assert_any_call("INSTALL icu")
        mock_conn.execute.assert_any_call("LOAD icu")
        mock_conn.close.assert_called_once()

    def test_ignores_already_installed_error(self, tmp_path):
        """ensure_icu_extension swallows exceptions (e.g., already installed)."""
        db_path = tmp_path / "data" / "warehouse.duckdb"
        db_path.parent.mkdir(parents=True)
        db_path.touch()

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("already installed")
        with patch("duckdb.connect", return_value=mock_conn):
            ensure_icu_extension(tmp_path)  # Should not raise

        mock_conn.close.assert_called_once()
