"""tests/unit/test_platform_startup.py

Tests for dango.platform.common.startup shared startup helpers.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.config.models import DangoConfig, PlatformSettings, ProjectContext
from dango.exceptions import VersionMismatchError
from dango.platform.common.startup import (
    _link_metabase_admin,
    check_duckdb_version_alignment,
    ensure_dbt_schemas,
    ensure_duckdb_driver,
    import_dashboards,
    run_pending_migrations,
    setup_metabase_if_needed,
    start_docker_services,
)
from dango.platform.docker import render_docker_compose
from dango.utils.driver import METABASE_DUCKDB_DRIVER_VERSION

# Patch target for NetworkConfig.get_project_info: dango.platform.local.network is the
# source module (metabase.py lazy-imports it inside _should_apply_local_site_url()
# rather than importing it at module level), so that's what must be patched — patching
# dango.visualization.metabase.NetworkConfig would silently no-op.
_NETWORK_CONFIG_GET_PROJECT_INFO = "dango.platform.local.network.NetworkConfig.get_project_info"


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
        (plugins_dir / ".driver-version").write_text(f"{METABASE_DUCKDB_DRIVER_VERSION}\n")

        with patch("urllib.request.urlretrieve") as mock_retrieve:
            ensure_duckdb_driver(tmp_path)

        mock_retrieve.assert_not_called()

    def test_downloads_driver_if_missing(self, tmp_path):
        """ensure_duckdb_driver calls urlretrieve when driver is absent."""

        def fake_retrieve(url: str, dest: Path) -> None:
            """Simulate download by creating the destination file."""
            Path(dest).touch()

        with patch("urllib.request.urlretrieve", side_effect=fake_retrieve):
            ensure_duckdb_driver(tmp_path)

        plugins_dir = tmp_path / "metabase-plugins"
        driver_path = plugins_dir / "duckdb.metabase-driver.jar"
        assert driver_path.exists()
        assert (
            plugins_dir / ".driver-version"
        ).read_text().strip() == METABASE_DUCKDB_DRIVER_VERSION

    def test_raises_runtime_error_after_3_failures(self, tmp_path):
        """ensure_duckdb_driver raises RuntimeError when all 3 attempts fail."""
        with patch("urllib.request.urlretrieve", side_effect=OSError("network error")):
            with patch("time.sleep"):  # Skip retry delays
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
            ensure_duckdb_driver(tmp_path)

        mock_ret.assert_called_once()
        assert (
            plugins_dir / ".driver-version"
        ).read_text().strip() == METABASE_DUCKDB_DRIVER_VERSION

    def test_redownloads_when_version_file_missing(self, tmp_path):
        """ensure_duckdb_driver re-downloads when version file is absent."""
        plugins_dir = tmp_path / "metabase-plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "duckdb.metabase-driver.jar").touch()

        def fake_retrieve(url: str, dest: Path) -> None:
            Path(dest).touch()

        with patch("urllib.request.urlretrieve", side_effect=fake_retrieve) as mock_ret:
            ensure_duckdb_driver(tmp_path)

        mock_ret.assert_called_once()
        assert (
            plugins_dir / ".driver-version"
        ).read_text().strip() == METABASE_DUCKDB_DRIVER_VERSION


def _make_config(metabase_port: int = 3000, dbt_docs_port: int = 8081) -> DangoConfig:
    return DangoConfig(
        project=ProjectContext(
            name="Test Project",
            purpose="testing",
            created_by="test-user",
        ),
        platform=PlatformSettings(metabase_port=metabase_port, dbt_docs_port=dbt_docs_port),
    )


@pytest.mark.unit
class TestRenderDockerCompose:
    def test_uses_current_config_ports(self, tmp_path: Path) -> None:
        """render_docker_compose() writes the ports from the passed-in
        config, not the PlatformSettings defaults -- this is the core of
        the bug fix: docker-compose.yml must reflect whatever config says
        *right now*, not whatever it said at `dango init` time."""
        config = _make_config(metabase_port=9999, dbt_docs_port=9998)
        render_docker_compose(tmp_path, config)

        content = (tmp_path / "docker-compose.yml").read_text()
        assert "127.0.0.1:9999:3000" in content
        assert "127.0.0.1:9998:80" in content
        assert "127.0.0.1:3000:3000" not in content
        assert "127.0.0.1:8081:80" not in content

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling render_docker_compose() twice with identical config
        produces byte-for-byte identical output -- the regression risk this
        task calls out: unconditional regeneration must be a true no-op
        when nothing has changed."""
        config = _make_config()
        render_docker_compose(tmp_path, config)
        first = (tmp_path / "docker-compose.yml").read_bytes()

        render_docker_compose(tmp_path, config)
        second = (tmp_path / "docker-compose.yml").read_bytes()

        assert first == second


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

        assert manager.stop_services.call_count == 2

    def test_reconciled_timeout_does_not_trigger_cleanup(self, tmp_path):
        """A manager that reconciles a late timeout to success is not stopped again."""
        manager = self._make_manager()
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", return_value=mock_sock):
                start_docker_services(tmp_path)

        # The first call is the normal pre-start cleanup. A reconciled
        # ``start_services() is True`` must not cause a second cleanup.
        assert manager.stop_services.call_count == 1

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

    def test_checks_configured_ports_not_hardcoded_defaults(self, tmp_path):
        """A project with custom metabase_port/dbt_docs_port must have the
        pre-flight check look at those ports, not the hardcoded 3000/8081
        defaults — otherwise a project configured to avoid a real conflict
        gets a misleading conflict report on the wrong ports entirely."""
        import yaml

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        project_data = {
            "project": {
                "name": "Custom Port Project",
                "created_by": "test@example.com",
                "purpose": "Testing custom ports",
            },
            "platform": {
                "metabase_port": 3001,
                "dbt_docs_port": 8082,
            },
        }
        with open(dango_dir / "project.yml", "w") as f:
            yaml.safe_dump(project_data, f, default_flow_style=False)
        with open(dango_dir / "sources.yml", "w") as f:
            yaml.safe_dump({"version": "1.0", "sources": []}, f)

        manager = self._make_manager()

        # Only port 3000 (the OLD hardcoded default) is occupied — the
        # configured port 3001 is free. If the fix works, this should NOT
        # raise, because the pre-flight check looks at 3001, not 3000.
        def fake_socket(*args, **kwargs):
            sock = MagicMock()
            sock.connect_ex.side_effect = lambda addr: 0 if addr[1] == 3000 else 1
            return sock

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", side_effect=fake_socket):
                start_docker_services(tmp_path)  # Should not raise — 3001 is free

        manager.start_services.assert_called_once()

    def test_falls_back_to_defaults_when_config_unavailable(self, tmp_path):
        """No project.yml present (e.g. called before a project exists) —
        must fall back to the same 3000/8081 defaults as before, not raise
        a config-loading exception through this function's RuntimeError-only
        contract."""
        manager = self._make_manager()

        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1  # ports free

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", return_value=mock_sock):
                start_docker_services(tmp_path)  # Should not raise

        manager.start_services.assert_called_once()

    def test_regenerates_compose_before_starting(self, tmp_path):
        """docker-compose.yml must be regenerated from the project's
        current config immediately before manager.start_services() runs --
        otherwise a metabase_port/dbt_docs_port change in project.yml would
        silently have no effect (this is the exact bug this task fixes:
        the pre-flight check above already reads the *new* port, but
        without this call, start_services() would still start containers
        from the *old*, frozen docker-compose.yml)."""
        import yaml

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        with open(dango_dir / "project.yml", "w") as f:
            yaml.safe_dump(
                {
                    "project": {
                        "name": "Test Project",
                        "created_by": "test@example.com",
                        "purpose": "testing",
                    },
                    "platform": {"metabase_port": 3005, "dbt_docs_port": 8085},
                },
                f,
            )
        with open(dango_dir / "sources.yml", "w") as f:
            yaml.safe_dump({"version": "1.0", "sources": []}, f)

        manager = self._make_manager()
        mock_sock = MagicMock()
        mock_sock.connect_ex.return_value = 1  # ports free

        call_order = []

        def start_services_side_effect():
            call_order.append("start_services")
            return True

        manager.start_services.side_effect = start_services_side_effect

        def fake_render(project_root, config):
            call_order.append("render_docker_compose")
            assert config.platform.metabase_port == 3005
            assert config.platform.dbt_docs_port == 8085

        with patch("dango.platform.DockerManager", return_value=manager):
            with patch("dango.platform.common.startup.socket.socket", return_value=mock_sock):
                with patch(
                    "dango.platform.docker.render_docker_compose", side_effect=fake_render
                ) as mock_render:
                    start_docker_services(tmp_path)

        mock_render.assert_called_once()
        assert call_order == ["render_docker_compose", "start_services"]


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

    def test_returns_failure_result_when_duckdb_not_connected(self, tmp_path, monkeypatch):
        """setup_metabase_if_needed returns failure dict without raising (BUG-105)."""
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "admin@test.com")
        setup_result = {
            "success": False,
            "duckdb_connected": False,
            "errors": ["connection refused"],
        }

        with patch("dango.visualization.metabase.setup_metabase", return_value=setup_result):
            result = setup_metabase_if_needed(tmp_path, "MyProject", None)

        assert result["success"] is False
        assert result["duckdb_connected"] is False
        assert result["already_configured"] is False

    def test_returns_errors_when_setup_fails_before_duckdb(self, tmp_path, monkeypatch):
        """Pre-DuckDB failures are returned in the result dict, not masked (BUG-105)."""
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "bad@example.com")
        setup_result = {
            "success": False,
            "duckdb_connected": False,
            "errors": ["Some setup error"],
        }

        with patch("dango.visualization.metabase.setup_metabase", return_value=setup_result):
            result = setup_metabase_if_needed(tmp_path, "MyProject", None)

        assert result["success"] is False
        assert "Some setup error" in result["errors"][0]

    def test_success(self, tmp_path, monkeypatch):
        """setup_metabase_if_needed returns result dict on successful first-run setup."""
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "admin@test.com")
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

    def test_calls_setup_metabase_with_configured_port(self, tmp_path, monkeypatch):
        """setup_metabase() must be called with the project's actual configured
        metabase_port, not the hardcoded localhost:3000 default — otherwise a
        project on a non-default port silently targets the wrong Metabase
        instance entirely (confirmed live: this exact scenario hit a real,
        unrelated Metabase instance during 1.0.8-S's manual verification)."""
        import yaml

        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "admin@test.com")
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        project_data = {
            "project": {
                "name": "Custom Port",
                "created_by": "test@example.com",
                "purpose": "Testing",
            },
            "platform": {"metabase_port": 3001},
        }
        with open(dango_dir / "project.yml", "w") as f:
            yaml.safe_dump(project_data, f)
        with open(dango_dir / "sources.yml", "w") as f:
            yaml.safe_dump({"version": "1.0", "sources": []}, f)

        setup_result = {"success": True, "duckdb_connected": True, "errors": []}
        with patch(
            "dango.visualization.metabase.setup_metabase", return_value=setup_result
        ) as mock_setup:
            setup_metabase_if_needed(tmp_path, "MyProject", None)

        _, kwargs = mock_setup.call_args
        assert kwargs["metabase_url"] == "http://localhost:3001"

    def test_falls_back_to_default_port_when_config_unavailable(self, tmp_path, monkeypatch):
        """No project.yml present — must fall back to localhost:3000, not raise
        a config-loading exception."""
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "admin@test.com")
        setup_result = {"success": True, "duckdb_connected": True, "errors": []}

        with patch(
            "dango.visualization.metabase.setup_metabase", return_value=setup_result
        ) as mock_setup:
            setup_metabase_if_needed(tmp_path, "MyProject", None)

        _, kwargs = mock_setup.call_args
        assert kwargs["metabase_url"] == "http://localhost:3000"

    def test_skips_when_no_admin_email(self, tmp_path, monkeypatch):
        """setup_metabase_if_needed skips when no admin email is available."""
        monkeypatch.delenv("DANGO_ADMIN_EMAIL", raising=False)
        result = setup_metabase_if_needed(tmp_path, "MyProject", None)
        assert result["skipped"] is True
        assert result["success"] is True

    def test_skips_admin_at_localhost(self, tmp_path, monkeypatch):
        """BUG-100: admin@localhost is filtered out for Metabase setup."""
        monkeypatch.delenv("DANGO_ADMIN_EMAIL", raising=False)
        # Create auth DB with admin@localhost
        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import create_user
        from dango.auth.models import Role, User
        from dango.migrations.runner import MigrationRunner

        db_path = get_auth_db_path(tmp_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
        MigrationRunner(
            db_path=db_path, db_name="auth", migrations_dir=migrations_dir
        ).apply_pending()
        create_user(
            db_path,
            User(email="admin@localhost", password_hash="$2b$12$fakehash", role=Role.ADMIN),
        )

        result = setup_metabase_if_needed(tmp_path, "MyProject", None)
        assert result["skipped"] is True
        assert result["success"] is True

    def test_skips_dotless_email_domain(self, tmp_path, monkeypatch):
        """BUG-100: Emails with dot-less domains (e.g. localhost) skip Metabase setup."""
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "user@localhost")
        result = setup_metabase_if_needed(tmp_path, "MyProject", None)
        assert result["skipped"] is True
        assert result["success"] is True

    def test_valid_email_domain_proceeds(self, tmp_path, monkeypatch):
        """Emails with proper domains (e.g. test.com) proceed to setup."""
        monkeypatch.setenv("DANGO_ADMIN_EMAIL", "admin@test.com")
        setup_result = {"success": True, "duckdb_connected": True, "errors": []}
        with patch("dango.visualization.metabase.setup_metabase", return_value=setup_result):
            result = setup_metabase_if_needed(tmp_path, "MyProject", None)
        assert result["success"] is True
        assert result.get("skipped") is None


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

    def test_import_dashboards_still_noops_with_no_directories(self, tmp_path):
        """Regression risk check: a project with neither dashboards/ nor
        metabase/ (the common case for most projects) must still no-op
        cleanly, returning None with zero side effects -- matching current
        behavior exactly, not just for the metabase/ case this task fixes."""
        result = import_dashboards(tmp_path)
        assert result is None

    def test_import_dashboards_still_finds_legacy_directory(self, tmp_path):
        """Backward compatible: the legacy bare dashboards/ directory (no
        metabase/ directory at all) must still trigger delegation, exactly
        as before this fix."""
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

    def test_import_dashboards_finds_metabase_directory(self, tmp_path):
        """1.0.8-Q10 fix #1 regression test: a project using the current
        `dango metabase save` export convention (metabase/dashboards/*.yml)
        but with no legacy dashboards/ directory at all must still trigger
        the wrapper to proceed and delegate to the real import function.

        This is the exact regression from the live incident: this must
        fail against the pre-fix code (which gated on the legacy dashboards/
        directory only and returned None immediately, silently) and pass
        against the fix."""
        metabase_dashboards_dir = tmp_path / "metabase" / "dashboards"
        metabase_dashboards_dir.mkdir(parents=True)
        (metabase_dashboards_dir / "overview.yml").touch()

        expected = {"imported": 1, "skipped": 0}
        with patch(
            "dango.visualization.dashboard_manager.import_dashboards", return_value=expected
        ) as mock_import:
            result = import_dashboards(tmp_path)

        assert result == expected
        mock_import.assert_called_once_with(tmp_path)

    def test_returns_none_when_metabase_dir_has_no_yml_files(self, tmp_path):
        """An empty metabase/ directory (e.g. only non-yml files) must still
        no-op cleanly, matching the legacy-directory no-op behavior."""
        metabase_dir = tmp_path / "metabase"
        metabase_dir.mkdir()
        (metabase_dir / "README.txt").touch()
        result = import_dashboards(tmp_path)
        assert result is None


@pytest.mark.unit
class TestLinkMetabaseAdmin:
    """Tests for _link_metabase_admin() SSO linking helper."""

    def _setup_metabase_yml(self, tmp_path: Path, email: str = "admin@test.com") -> None:
        """Write a minimal metabase.yml fixture."""
        import yaml

        d = tmp_path / ".dango"
        d.mkdir(exist_ok=True)
        (d / "metabase.yml").write_text(
            yaml.safe_dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": email, "password": "testpw"},
                    "database": {"id": 1, "name": "Test"},
                }
            )
        )

    def _setup_auth_db(self, tmp_path: Path, email: str = "admin@test.com") -> None:
        """Create auth.db with an admin user that has no metabase_user_id."""
        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import create_user
        from dango.auth.models import Role, User
        from dango.migrations.runner import MigrationRunner

        db_path = get_auth_db_path(tmp_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
        MigrationRunner(
            db_path=db_path, db_name="auth", migrations_dir=migrations_dir
        ).apply_pending()
        user = User(email=email, password_hash="$2b$12$fakehash", role=Role.ADMIN)
        create_user(db_path, user)

    @patch("requests.put")
    @patch("requests.post")
    @patch("requests.get")
    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    def test_happy_path_links_admin(
        self,
        mock_sts: MagicMock,
        mock_get: MagicMock,
        mock_post: MagicMock,
        mock_put: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Links Metabase admin to Dango admin when both exist and not yet linked."""
        self._setup_metabase_yml(tmp_path)
        self._setup_auth_db(tmp_path)
        mock_sts.return_value.encrypt_token.return_value = "encrypted_pw"

        # Mock Metabase API responses
        session_resp = MagicMock(status_code=200)
        session_resp.json.return_value = {"id": "session123"}
        mock_post.return_value = session_resp

        user_list_resp = MagicMock(status_code=200)
        user_list_resp.json.return_value = [{"id": 42, "email": "admin@test.com"}]
        mock_get.return_value = user_list_resp

        mock_put.return_value = MagicMock(status_code=200)

        _link_metabase_admin(tmp_path, "admin@test.com")

        # Verify correct password endpoint was called with old_password
        mock_put.assert_called_once()
        put_url, put_kwargs = mock_put.call_args[0][0], mock_put.call_args[1]
        assert "/api/user/42/password" in put_url
        assert "old_password" in put_kwargs.get("json", {})
        assert put_kwargs["json"]["old_password"] == "testpw"  # from metabase.yml fixture

        # Verify user was updated in auth.db
        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import get_user_by_email

        user = get_user_by_email(get_auth_db_path(tmp_path), "admin@test.com")
        assert user is not None
        assert user.metabase_user_id == 42
        assert user.metabase_password_enc == "encrypted_pw"

    @patch("requests.post")
    def test_skips_when_no_metabase_yml(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """Returns silently when metabase.yml doesn't exist."""
        (tmp_path / ".dango").mkdir(exist_ok=True)
        _link_metabase_admin(tmp_path, "admin@test.com")
        mock_post.assert_not_called()

    @patch("requests.put")
    @patch("requests.get")
    @patch("requests.post")
    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    def test_skips_when_already_linked(
        self,
        mock_sts: MagicMock,
        mock_post: MagicMock,
        mock_get: MagicMock,
        mock_put: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Returns silently when Dango admin already has metabase_user_id."""
        self._setup_metabase_yml(tmp_path)
        self._setup_auth_db(tmp_path)

        # Set metabase_user_id on the admin user
        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import get_user_by_email, update_user
        from dango.auth.models import UserUpdate

        db_path = get_auth_db_path(tmp_path)
        user = get_user_by_email(db_path, "admin@test.com")
        assert user is not None
        update_user(db_path, user.id, UserUpdate(metabase_user_id=99))

        session_resp = MagicMock(status_code=200)
        session_resp.json.return_value = {"id": "session123"}
        mock_post.return_value = session_resp

        _link_metabase_admin(tmp_path, "admin@test.com")

        # Should not attempt to find or update Metabase user
        mock_get.assert_not_called()
        mock_put.assert_not_called()

    @patch("requests.post")
    def test_skips_when_metabase_yml_malformed(self, mock_post: MagicMock, tmp_path: Path) -> None:
        """Returns silently when metabase.yml has no admin credentials."""
        import yaml

        d = tmp_path / ".dango"
        d.mkdir(exist_ok=True)
        (d / "metabase.yml").write_text(yaml.safe_dump({"metabase_url": "http://localhost:3000"}))

        _link_metabase_admin(tmp_path, "admin@test.com")
        mock_post.assert_not_called()

    @patch("requests.put")
    @patch("requests.post")
    @patch("requests.get")
    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    def test_happy_path_updates_metabase_yml(
        self,
        mock_sts: MagicMock,
        mock_get: MagicMock,
        mock_post: MagicMock,
        mock_put: MagicMock,
        tmp_path: Path,
    ) -> None:
        """BUG-115: metabase.yml is updated with the new password after linking."""
        import yaml

        self._setup_metabase_yml(tmp_path)
        self._setup_auth_db(tmp_path)
        mock_sts.return_value.encrypt_token.return_value = "encrypted_pw"

        session_resp = MagicMock(status_code=200)
        session_resp.json.return_value = {"id": "session123"}
        mock_post.return_value = session_resp

        user_list_resp = MagicMock(status_code=200)
        user_list_resp.json.return_value = [{"id": 42, "email": "admin@test.com"}]
        mock_get.return_value = user_list_resp

        mock_put.return_value = MagicMock(status_code=200)

        _link_metabase_admin(tmp_path, "admin@test.com")

        # Verify metabase.yml was updated with the new password
        mb_yml_path = tmp_path / ".dango" / "metabase.yml"
        with open(mb_yml_path) as f:
            updated_creds = yaml.safe_load(f)
        new_password = updated_creds["admin"]["password"]
        assert new_password != "testpw", "Password in metabase.yml should have been updated"
        # The new password was sent to the Metabase API
        put_json = mock_put.call_args[1]["json"]
        assert put_json["password"] == new_password

    @patch("requests.put")
    @patch("requests.post")
    @patch("requests.get")
    @patch("dango.auth.metabase_sync.SecureTokenStorage")
    def test_metabase_yml_unchanged_when_password_update_fails(
        self,
        mock_sts: MagicMock,
        mock_get: MagicMock,
        mock_post: MagicMock,
        mock_put: MagicMock,
        tmp_path: Path,
    ) -> None:
        """BUG-115: metabase.yml keeps old password when Metabase password update fails."""
        import yaml

        self._setup_metabase_yml(tmp_path)
        self._setup_auth_db(tmp_path)

        session_resp = MagicMock(status_code=200)
        session_resp.json.return_value = {"id": "session123"}
        mock_post.return_value = session_resp

        user_list_resp = MagicMock(status_code=200)
        user_list_resp.json.return_value = [{"id": 42, "email": "admin@test.com"}]
        mock_get.return_value = user_list_resp

        # Simulate password update failure (returns 400)
        mock_put.return_value = MagicMock(status_code=400)

        _link_metabase_admin(tmp_path, "admin@test.com")

        # Verify metabase.yml was NOT changed
        mb_yml_path = tmp_path / ".dango" / "metabase.yml"
        with open(mb_yml_path) as f:
            unchanged_creds = yaml.safe_load(f)
        assert unchanged_creds["admin"]["password"] == "testpw"


@pytest.mark.unit
class TestRefreshMetabaseConnection:
    """Tests for refresh_metabase_connection() container name resolution (BUG-118)."""

    def test_unexpected_error_returns_gracefully_instead_of_masking_typeerror(
        self, tmp_path: Path
    ) -> None:
        """1.0.8-W regression test: the outer `except Exception as e: logger.warning(...)`
        block used to pass `error=str(e)` to stdlib logging.Logger.warning(), which isn't
        a valid kwarg there (this module's `logger` is `logging.getLogger`, not
        structlog) -- verified live to raise its own TypeError, silently replacing
        whatever the real exception was. Positive control: force an exception inside the
        try block and confirm the function now returns (False, <message>) instead of
        raising."""
        from dango.visualization.metabase import refresh_metabase_connection

        with patch("dango.platform.docker.DockerManager", side_effect=RuntimeError("boom")):
            result = refresh_metabase_connection(tmp_path)

        assert result == (False, "boom", None)

    def test_uses_hash_based_container_name(self, tmp_path: Path) -> None:
        """BUG-118: refresh_metabase_connection uses DockerManager's hash-based name."""
        from dango.visualization.metabase import refresh_metabase_connection

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("requests.Session.get", return_value=MagicMock(status_code=200)),
            # 1.0.8-Q17: readiness is now a docker-logs poll, not a login --
            # mock it directly rather than faking subprocess.run's 3rd call
            # (docker logs), which mock_subprocess_run's 2-item side_effect
            # list above doesn't provide.
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
        ):
            result = refresh_metabase_connection(tmp_path)

        # Verify the hash-based container name was used in docker ps filter
        ps_call = mock_subprocess_run.call_args_list[0]
        ps_cmd = ps_call[0][0]
        assert "name=dango-abc123-metabase-1" in " ".join(ps_cmd)

        restart_call = mock_subprocess_run.call_args_list[1]
        assert restart_call[0][0] == ["docker", "restart", "dango-abc123-metabase-1"]
        # tmp_path has no .dango/metabase.yml at all, so the post-readiness
        # login is skipped entirely (no credentials to log in with) --
        # session_id is None, not a failure.
        assert result == (True, None, None)

    def test_uses_configured_url_from_creds_file_when_not_passed(self, tmp_path: Path) -> None:
        """1.0.8-fix: when the caller doesn't pass metabase_url explicitly, it must
        be resolved from .dango/metabase.yml's own "metabase_url" key (same
        precedent as sync_metabase_schema()/set_metabase_telemetry()), not always
        default to localhost:3000 -- a project on a non-default
        platform.metabase_port had every automatic post-sync refresh silently
        target the wrong host. See BUGS-FOUND.md.

        1.0.8-AH: the log-ready check is mocked False here (not True, as most
        other tests in this class use) so the /api/health fallback actually
        runs and its URL can be asserted -- with log_ready=True the health
        check is never called at all, so there'd be nothing to assert
        the resolved URL against.
        """
        import yaml as yaml_module

        from dango.visualization.metabase import refresh_metabase_connection

        creds_file = tmp_path / ".dango" / "metabase.yml"
        creds_file.parent.mkdir(parents=True)
        creds_file.write_text(yaml_module.dump({"metabase_url": "http://localhost:13000"}))

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )
        mock_get = MagicMock(return_value=MagicMock(status_code=200))

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("requests.Session.get", mock_get),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
        ):
            result = refresh_metabase_connection(tmp_path)

        assert result == (True, None, None)
        assert mock_get.call_args[0][0] == "http://localhost:13000/api/health"

    def test_explicit_metabase_url_wins_over_creds_file(self, tmp_path: Path) -> None:
        """An explicitly-passed metabase_url still takes priority over the creds
        file's own value.

        1.0.8-AH: log-ready mocked False (see comment on the test above) so
        the /api/health fallback runs and its URL can be asserted.
        """
        import yaml as yaml_module

        from dango.visualization.metabase import refresh_metabase_connection

        creds_file = tmp_path / ".dango" / "metabase.yml"
        creds_file.parent.mkdir(parents=True)
        creds_file.write_text(yaml_module.dump({"metabase_url": "http://localhost:13000"}))

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),
                MagicMock(returncode=0),
            ]
        )
        mock_get = MagicMock(return_value=MagicMock(status_code=200))

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("requests.Session.get", mock_get),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
        ):
            refresh_metabase_connection(tmp_path, metabase_url="http://localhost:19999")

        assert mock_get.call_args[0][0] == "http://localhost:19999/api/health"

    def test_returns_false_when_container_not_running(self, tmp_path: Path) -> None:
        """Returns False when the Metabase container is not found."""
        from dango.visualization.metabase import refresh_metabase_connection

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", return_value=MagicMock(stdout="", returncode=0)),
        ):
            result = refresh_metabase_connection(tmp_path)
        assert result[0] is False
        assert result[1] == "Metabase container not running"
        assert result[2] is None

    def test_reapplies_site_url_on_successful_restart(self, tmp_project_dir: Path) -> None:
        """1.0.8-W: an already-configured project (metabase.yml predates the Site URL
        fix, no site_url_set marker yet) gets the fix applied via this path exactly
        once, since setup_metabase() runs once and never retroactively fixes existing
        projects. tmp_project_dir gives config.platform.port == 8800 (default, no
        platform: override). A successful PUT must also persist site_url_set: true so
        future syncs skip the login+PUT entirely (see the marker-skip test below)."""
        import requests
        import yaml as yaml_module

        from dango.visualization.metabase import refresh_metabase_connection

        creds_file = tmp_project_dir / ".dango" / "metabase.yml"
        creds_file.write_text(
            yaml_module.dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": "admin@example.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                }
            )
        )

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = MagicMock(status_code=200)  # /api/health

        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-xyz"}
        mock_session.post.return_value = login_resp  # /api/session (login)

        site_url_resp = MagicMock(status_code=200)
        mock_session.put.return_value = site_url_resp  # /api/setting/site-url

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.config.helpers.is_cloud_mode", return_value=False),
            patch(_NETWORK_CONFIG_GET_PROJECT_INFO, return_value=None),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
        ):
            result = refresh_metabase_connection(tmp_project_dir)

        assert result == (True, None, "sess-xyz")
        mock_session.put.assert_called_once_with(
            "http://localhost:3000/api/setting/site-url",
            headers={"X-Metabase-Session": "sess-xyz"},
            json={"value": "http://localhost:8800/metabase/"},
            timeout=10,
        )
        assert yaml_module.safe_load(creds_file.read_text())["site_url_set"] is True
        # 1.0.8-Q17: only ONE real login now -- refresh_metabase_connection()'s
        # own post-restart login token is reused by the site-url catch-up
        # instead of it logging in a second time.
        assert mock_session.post.call_count == 1

    def test_site_url_failure_does_not_block_refresh(self, tmp_project_dir: Path) -> None:
        """A login failure (so the site-url PUT never even gets attempted) must not
        turn a successful container restart into a reported failure, and must not
        mark site_url_set so the next sync retries it.

        1.0.8-Q17 rewrite: readiness is now a docker-logs poll (mocked True here,
        isolating this test to the login-failure behavior specifically -- the
        log-based readiness check itself has its own direct tests below). Login
        is now a single attempt (no retry loop to bound/count), consistent with
        this module's other single-attempt login call sites -- Q11/Q14/Q16's
        login-retry-loop budget no longer exists to tune or trip a lockout with.
        """
        import requests
        import yaml as yaml_module

        from dango.visualization.metabase import refresh_metabase_connection

        creds_file = tmp_project_dir / ".dango" / "metabase.yml"
        creds_file.write_text(
            yaml_module.dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": "admin@example.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                }
            )
        )

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = MagicMock(status_code=200)  # /api/health
        # Login fails outright, every attempt.
        mock_session.post.return_value = MagicMock(status_code=401)

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.config.helpers.is_cloud_mode", return_value=False),
            patch(_NETWORK_CONFIG_GET_PROJECT_INFO, return_value=None),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
        ):
            result = refresh_metabase_connection(tmp_project_dir)

        # Restart itself still reported successful; session_id is None since the
        # login failed.
        assert result == (True, None, None)
        # 2 login attempts total: refresh_metabase_connection()'s own, plus the
        # site-url catch-up's fallback login (since it was passed session_id=None
        # -- the first login failed). Not 6+ like the old retry-loop design.
        assert mock_session.post.call_count == 2
        mock_session.put.assert_not_called()
        assert "site_url_set" not in yaml_module.safe_load(creds_file.read_text())

    def test_skips_site_url_when_already_marked_set(self, tmp_project_dir: Path) -> None:
        """1.0.8-W: once site_url_set is true (written by a prior successful
        setup_metabase() or refresh_metabase_connection() call), the site-url
        catch-up must not repeat its own login + PUT -- that would retransmit the
        admin password on every single sync forever, for zero benefit once it's
        already correct.

        1.0.8-Q17: refresh_metabase_connection() itself still does ONE real login
        unconditionally (regardless of site_url_set) to obtain a token for
        downstream callers like sync_metabase_schema() -- that single login is
        asserted below, distinguishing it from the site-url catch-up adding a
        SECOND one (which it must not, since it returns early on the
        site_url_set marker before ever looking at the passed-in session_id).
        """
        import requests
        import yaml as yaml_module

        from dango.visualization.metabase import refresh_metabase_connection

        creds_file = tmp_project_dir / ".dango" / "metabase.yml"
        creds_file.write_text(
            yaml_module.dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": "admin@example.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                    "site_url_set": True,
                }
            )
        )

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = MagicMock(status_code=200)  # /api/health
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-marked"}
        mock_session.post.return_value = login_resp

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.config.helpers.is_cloud_mode", return_value=False),
            patch(_NETWORK_CONFIG_GET_PROJECT_INFO, return_value=None),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
        ):
            result = refresh_metabase_connection(tmp_project_dir)

        assert result == (True, None, "sess-marked")
        # Exactly one real login: refresh_metabase_connection()'s own. The
        # site-url catch-up's early-return on site_url_set means it never adds
        # a second one.
        assert mock_session.post.call_count == 1
        mock_session.put.assert_not_called()  # no site-url PUT

    def test_skips_site_url_in_cloud_mode(self, tmp_project_dir: Path) -> None:
        """cloud_mode must skip the site-url login+PUT entirely (not just the PUT)
        -- Caddy fronts Metabase with a real public domain there, and there's no
        reason to pay a second login for a value this function isn't going to
        write anyway. refresh_metabase_connection()'s own single unconditional
        login (for downstream token reuse) still happens regardless of
        cloud_mode -- asserted below as exactly one call, not zero."""
        import requests
        import yaml as yaml_module

        from dango.visualization.metabase import refresh_metabase_connection

        creds_file = tmp_project_dir / ".dango" / "metabase.yml"
        creds_file.write_text(
            yaml_module.dump(
                {
                    "metabase_url": "http://localhost:3000",
                    "admin": {"email": "admin@example.com", "password": "secret"},
                    "database": {"id": 5, "name": "Test Analytics"},
                }
            )
        )

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        mock_session = MagicMock(spec=requests.Session)
        mock_session.get.return_value = MagicMock(status_code=200)  # /api/health
        login_resp = MagicMock(status_code=200)
        login_resp.json.return_value = {"id": "sess-cloud"}
        mock_session.post.return_value = login_resp

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("dango.visualization.metabase.requests.Session", return_value=mock_session),
            patch("dango.config.helpers.is_cloud_mode", return_value=True),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
        ):
            result = refresh_metabase_connection(tmp_project_dir)

        assert result == (True, None, "sess-cloud")
        assert mock_session.post.call_count == 1  # refresh's own login only
        mock_session.put.assert_not_called()  # no site-url PUT in cloud mode

    def test_session_id_none_when_login_never_attempted(self, tmp_path: Path) -> None:
        """1.0.8-Q17: the 3-tuple's session_id is None (not an error) when there
        were simply no credentials to log in with -- distinct from a login that
        was attempted and failed (covered by test_site_url_failure_does_not_block_refresh
        above), but both land on session_id=None for the same reason downstream
        callers fall back to their own login either way."""
        from dango.visualization.metabase import refresh_metabase_connection

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("requests.Session.get", return_value=MagicMock(status_code=200)),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
        ):
            result = refresh_metabase_connection(tmp_path)  # no .dango/metabase.yml at all

        assert result == (True, None, None)

    def test_refresh_metabase_connection_succeeds_via_log_check(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """1.0.8-AH: the log-line-based check is tried first and, when it
        confirms readiness, short-circuits the /api/health fallback entirely
        -- mirroring AG's test_setup_metabase_uses_log_ready_check_first.
        Asserts session.get is never called for /api/health, not just that
        the return value is correct. Also asserts NO fallback warning is
        logged here -- negative control paired with the positive control in
        test_refresh_metabase_connection_falls_back_to_health_check, so the
        two tests together prove the warning actually distinguishes which
        check fired instead of firing unconditionally."""
        from dango.visualization.metabase import refresh_metabase_connection

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )
        mock_get = MagicMock(return_value=MagicMock(status_code=200))

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("requests.Session.get", mock_get),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=True),
            caplog.at_level("WARNING", logger="dango.visualization.metabase"),
        ):
            result = refresh_metabase_connection(tmp_path)  # no .dango/metabase.yml at all

        assert result == (True, None, None)
        mock_get.assert_not_called()
        assert not any("/api/health fallback" in record.message for record in caplog.records)

    def test_refresh_metabase_connection_falls_back_to_health_check(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """1.0.8-AH: when the log check doesn't confirm readiness, the
        /api/health poll is the fallback and a 200 response still yields a
        successful result. Also asserts the fallback-succeeded case is
        logged (not silent) -- this project's own review history flagged an
        earlier version of this diff for dropping observability into exactly
        this scenario (log check never confirmed, but Metabase does appear
        to be up per /api/health) without noticing the substantive case was
        still reachable, just via a different branch."""
        from dango.visualization.metabase import refresh_metabase_connection

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch("requests.Session.get", return_value=MagicMock(status_code=200)),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            caplog.at_level("WARNING", logger="dango.visualization.metabase"),
        ):
            result = refresh_metabase_connection(tmp_path)  # no .dango/metabase.yml at all

        assert result == (True, None, None)
        assert any("/api/health fallback" in record.message for record in caplog.records), (
            "expected a warning logging that readiness was confirmed via the /api/health fallback"
        )

    def test_refresh_metabase_connection_fails_when_both_checks_fail(self, tmp_path: Path) -> None:
        """1.0.8-AH: when neither the log check nor the /api/health poll ever
        confirms readiness within max_wait_seconds, the function reports
        failure -- the same error message as before this change. time.sleep
        is mocked so this doesn't actually wait real wall-clock time."""
        import requests

        from dango.visualization.metabase import refresh_metabase_connection

        mock_dm = MagicMock()
        mock_dm.compose_project_name = "dango-abc123"

        mock_subprocess_run = MagicMock(
            side_effect=[
                MagicMock(stdout="dango-abc123-metabase-1\n", returncode=0),  # docker ps
                MagicMock(returncode=0),  # docker restart
            ]
        )

        # time.monotonic() must actually advance past the deadline across
        # repeated calls (the fallback loop's `while time.monotonic() <
        # deadline`) -- a fixed return_value would spin the mocked
        # time.sleep() forever instead of exiting the loop.
        monotonic_values = iter([0.0, 0.0, 61.0])

        with (
            patch("dango.platform.docker.DockerManager", return_value=mock_dm),
            patch("subprocess.run", mock_subprocess_run),
            patch(
                "requests.Session.get",
                side_effect=requests.exceptions.RequestException("connection refused"),
            ),
            patch("dango.visualization.metabase._wait_for_metabase_log_ready", return_value=False),
            patch("time.monotonic", side_effect=lambda: next(monotonic_values)),
            patch("time.sleep"),
        ):
            result = refresh_metabase_connection(tmp_path)  # no .dango/metabase.yml at all

        assert result == (False, "Metabase did not become healthy after restart", None)


@pytest.mark.unit
class TestWaitForMetabaseLogReady:
    """Direct unit tests for _wait_for_metabase_log_ready() (1.0.8-Q17) -- the
    docker-logs-based restart-readiness check that replaced this module's prior
    login-based readiness poll (removed by this task; see its history in
    BUGS-FOUND.md: 1.0.8-Q11/Q14/Q16 all tried login-based versions of this
    check and either failed to close the race or tripped Metabase's own
    login-throttle lockout)."""

    def test_returns_true_promptly_when_line_present(self) -> None:
        from dango.visualization.metabase import _wait_for_metabase_log_ready

        log_output = MagicMock(
            stdout=(
                "2026-09-15 10:00:00,000 INFO core.core :: Metabase Initialization "
                "COMPLETE in 3.4 s (JVM uptime: 16.1 s)\n"
            ),
            stderr="",
        )

        with (
            patch("subprocess.run", return_value=log_output) as mock_run,
            patch("dango.visualization.metabase.time.sleep") as mock_sleep,
        ):
            result = _wait_for_metabase_log_ready("dango-abc-metabase-1", "2026-09-15T10:00:00Z")

        assert result is True
        # Found on the very first poll -- no retry sleep needed.
        mock_sleep.assert_not_called()
        docker_cmd = mock_run.call_args[0][0]
        assert docker_cmd == [
            "docker",
            "logs",
            "--since",
            "2026-09-15T10:00:00Z",
            "dango-abc-metabase-1",
        ]

    def test_checks_stderr_too(self) -> None:
        """The line is confirmed live to be on stdout for this module's own
        Metabase image (see the function's docstring), but this isn't a contract
        Dango controls -- confirm the stderr fallback path also works."""
        from dango.visualization.metabase import _wait_for_metabase_log_ready

        log_output = MagicMock(
            stdout="",
            stderr="INFO core.core :: Metabase Initialization COMPLETE in 2.1 s\n",
        )

        with patch("subprocess.run", return_value=log_output):
            result = _wait_for_metabase_log_ready("dango-abc-metabase-1", "2026-09-15T10:00:00Z")

        assert result is True

    def test_returns_false_after_timeout_when_line_never_appears(self) -> None:
        from dango.visualization.metabase import _wait_for_metabase_log_ready

        no_match = MagicMock(stdout="some other unrelated log line\n", stderr="")

        # time.monotonic() call #1 sets the deadline (0 + 90 = 90). Calls #2-#4
        # (values 1, 2, 3) each pass the `< 90` while-condition check, driving one
        # subprocess.run + sleep per iteration (3 total). Call #5 (value 100)
        # fails the condition and the loop exits without a 4th subprocess.run.
        with (
            patch("subprocess.run", return_value=no_match) as mock_run,
            patch("dango.visualization.metabase.time.sleep"),
            patch(
                "dango.visualization.metabase.time.monotonic",
                side_effect=[0, 1, 2, 3, 100],
            ),
        ):
            result = _wait_for_metabase_log_ready(
                "dango-abc-metabase-1", "2026-09-15T10:00:00Z", max_wait_seconds=90
            )

        assert result is False
        assert mock_run.call_count == 3

    def test_stale_line_from_prior_boot_does_not_false_positive_without_since(self) -> None:
        """Sanity check on the false-positive this function's docstring warns
        about: a log blob containing the target line always matches, regardless
        of timestamp, which is exactly why callers MUST pass a `since` captured
        right before the restart -- `docker logs --since` is what filters out a
        stale line, not anything in this function's own string search. This test
        documents that contract by showing the function trusts `since` completely
        (it's passed straight to the `docker logs` subprocess call, not
        re-validated), so passing a stale `since` would defeat the guarantee."""
        from dango.visualization.metabase import _wait_for_metabase_log_ready

        stale_and_fresh = MagicMock(
            stdout=(
                "2026-09-15 09:00:00,000 INFO core.core :: Metabase Initialization "
                "COMPLETE in 3.0 s (JVM uptime: 10.0 s)\n"  # from a PRIOR boot
            ),
            stderr="",
        )

        with patch("subprocess.run", return_value=stale_and_fresh) as mock_run:
            result = _wait_for_metabase_log_ready(
                "dango-abc-metabase-1", since="2026-09-15T08:00:00Z"
            )

        # This function has no way to tell a "stale" line from a "fresh" one on
        # its own -- it relies entirely on `docker logs --since` (passed the
        # `since` argument) to have already excluded prior-boot lines from
        # `result.stdout` before this function ever sees it. Confirms `since`
        # actually reaches the subprocess call unmodified.
        assert result is True
        assert mock_run.call_args[0][0][3] == "2026-09-15T08:00:00Z"  # index 2 is "--since" itself

    def test_returns_false_on_subprocess_failure_without_raising(self) -> None:
        from dango.visualization.metabase import _wait_for_metabase_log_ready

        with (
            patch("subprocess.run", side_effect=OSError("docker not found")),
            patch("dango.visualization.metabase.time.sleep"),
            patch(
                "dango.visualization.metabase.time.monotonic",
                side_effect=[0, 1, 100],
            ),
        ):
            result = _wait_for_metabase_log_ready("dango-abc-metabase-1", "2026-09-15T10:00:00Z")

        assert result is False


@pytest.mark.unit
class TestCheckDuckdbVersionAlignment:
    """Tests for the startup wrapper around driver.check_version_alignment."""

    def test_calls_through_to_driver(self):
        """Wrapper delegates to driver.check_version_alignment."""
        with patch("dango.utils.driver.check_version_alignment") as mock_check:
            check_duckdb_version_alignment()
        mock_check.assert_called_once()

    def test_propagates_mismatch_error(self):
        """VersionMismatchError from driver propagates through wrapper."""
        with patch(
            "dango.utils.driver.check_version_alignment",
            side_effect=VersionMismatchError("test mismatch"),
        ):
            with pytest.raises(VersionMismatchError, match="test mismatch"):
                check_duckdb_version_alignment()
