"""tests/unit/test_platform_startup.py

Tests for dango.platform.common.startup shared startup helpers.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.common.startup import (
    _link_metabase_admin,
    ensure_dbt_schemas,
    ensure_duckdb_driver,
    import_dashboards,
    run_pending_migrations,
    setup_metabase_if_needed,
    start_docker_services,
)
from dango.utils.driver import METABASE_DUCKDB_DRIVER_VERSION


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

    def test_skips_when_no_admin_email(self, tmp_path, monkeypatch):
        """setup_metabase_if_needed skips when no admin email is available."""
        monkeypatch.delenv("DANGO_ADMIN_EMAIL", raising=False)
        result = setup_metabase_if_needed(tmp_path, "MyProject", None)
        assert result["skipped"] is True
        assert result["success"] is True

    def test_skips_admin_at_localhost(self, tmp_path, monkeypatch):
        """BUG-100: admin@localhost is filtered like admin@dango.local."""
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
