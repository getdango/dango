"""dango/platform/common/startup.py

Shared startup helpers for platform lifecycle.

Contains logic extracted from cli/commands/platform.py for reuse by both
`dango start` (local) and `dango serve` (cloud, TASK-026). Most functions
raise exceptions on failure; callers handle display and user interaction.

Exception: ``rotate_logs()`` follows the never-fail contract — errors are
logged as warnings, never raised.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any


def rotate_logs(project_root: Path) -> None:
    """Rotate JSONL log files if size or age thresholds are exceeded.

    Rotates ``audit.jsonl`` and ``activity.jsonl`` using gzip compression.
    Never raises — delegates to never-fail rotation functions.

    Args:
        project_root: Project root directory.
    """
    from dango.utils.log_rotation import rotate_jsonl_log

    log_dir = project_root / ".dango" / "logs"
    rotate_jsonl_log(log_dir / "audit.jsonl")
    rotate_jsonl_log(log_dir / "activity.jsonl")


def run_pending_migrations(project_root: Path) -> dict[str, Any]:
    """
    Run all pending database migrations.

    Args:
        project_root: Project root directory

    Returns:
        Dict mapping db name to list of MigrationInfo objects for applied migrations.
        Empty dict if no migrations applied.

    Raises:
        MigrationError: If a migration fails
    """
    from dango.migrations import apply_all_pending

    return apply_all_pending(project_root)


def ensure_dbt_schemas(project_root: Path) -> None:
    """
    Create DuckDB schemas required for Metabase visibility.

    Args:
        project_root: Project root directory
    """
    from dango.utils.database import ensure_dbt_schemas as _ensure_dbt_schemas

    duckdb_path = project_root / "data" / "warehouse.duckdb"
    _ensure_dbt_schemas(duckdb_path)


def ensure_duckdb_driver(project_root: Path) -> None:
    """Ensure Metabase DuckDB driver is downloaded and version-matched.

    Downloads the driver if not present or if the installed DuckDB version
    has changed since the last download.  Retries 3 times on network failure.

    Args:
        project_root: Project root directory

    Raises:
        RuntimeError: If download fails after all retries
    """
    import time
    import urllib.request

    from dango.utils.driver import (
        METABASE_DUCKDB_DRIVER_VERSION,
        driver_needs_update,
        get_duckdb_driver_url,
        write_driver_version,
    )

    plugins_dir = project_root / "metabase-plugins"
    driver_path = plugins_dir / "duckdb.metabase-driver.jar"

    if driver_path.exists() and not driver_needs_update(plugins_dir):
        return

    # Delete stale driver if version mismatch
    if driver_path.exists():
        driver_path.unlink()

    driver_url = get_duckdb_driver_url()
    plugins_dir.mkdir(exist_ok=True)

    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2)
            urllib.request.urlretrieve(driver_url, driver_path)
            write_driver_version(plugins_dir, METABASE_DUCKDB_DRIVER_VERSION)
            return
        except Exception:
            if attempt == 2:
                # Clean up partial download on final failure
                try:
                    if driver_path.exists():
                        driver_path.unlink()
                except Exception:
                    pass

    raise RuntimeError(
        f"Failed to download DuckDB driver after 3 attempts. "
        f"URL: {driver_url} — "
        f"Check your internet connection and try again, or download manually from: "
        f"https://github.com/motherduckdb/metabase_duckdb_driver/releases"
    )


def start_docker_services(project_root: Path) -> None:
    """
    Pre-check Docker environment and start Docker services.

    Stops any existing Dango services, verifies Docker daemon is running,
    checks required port availability, then starts Metabase and dbt-docs.

    Args:
        project_root: Project root directory

    Raises:
        RuntimeError: If Docker daemon is not running, required ports are
            still occupied after cleanup, or services fail to start
    """
    from dango.platform import DockerManager

    manager = DockerManager(project_root)

    # Stop any existing services to free ports and clean up zombie containers
    manager.stop_services()

    # Pre-flight: Docker daemon must be running
    if not manager.is_docker_daemon_running():
        raise RuntimeError("Docker daemon is not running. Start Docker Desktop and try again.")

    # Pre-flight: Required Docker ports must be free
    required_docker_ports = {
        3000: "Metabase",
        8081: "dbt-docs",
    }

    ports_in_use = []
    for docker_port, service_name in required_docker_ports.items():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port_available = sock.connect_ex(("127.0.0.1", docker_port)) != 0
        sock.close()
        if not port_available:
            ports_in_use.append((docker_port, service_name))

    if ports_in_use:
        # Attempt to stop Dango containers from other projects
        manager.stop_all_dango_containers()

        # Recheck after cleanup
        ports_still_in_use = []
        for docker_port, service_name in required_docker_ports.items():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            port_available = sock.connect_ex(("127.0.0.1", docker_port)) != 0
            sock.close()
            if not port_available:
                ports_still_in_use.append((docker_port, service_name))

        if ports_still_in_use:
            port_list = ", ".join(f"{p} ({s})" for p, s in ports_still_in_use)
            raise RuntimeError(
                f"Required ports are still in use after cleanup: {port_list}. "
                "Run: lsof -ti:<port> | xargs kill -9"
            )

    # Start Docker services (Metabase, dbt-docs)
    docker_success = manager.start_services()
    if not docker_success:
        manager.stop_services()  # Clean up partial containers
        raise RuntimeError("Docker services failed to start. Check Docker logs: docker ps -a")


def setup_metabase_if_needed(
    project_root: Path,
    project_name: str,
    organization: str | None,
) -> dict[str, Any]:
    """
    Configure Metabase on first run.

    No-op if credentials file already exists. On first run, performs
    auto-setup and configures DuckDB connection.

    Args:
        project_root: Project root directory
        project_name: Project name for Metabase collections
        organization: Organization name (optional)

    Returns:
        Dict with keys:
            - already_configured (bool): True if credentials file existed
            - success (bool): Whether setup succeeded
            - collections_created (list): Collection names created
            - errors (list): Errors encountered during setup
            - duckdb_connected (bool): Whether DuckDB connection succeeded
    """
    import os

    from dango.config.helpers import is_cloud_mode
    from dango.visualization.metabase import setup_metabase

    credentials_file = project_root / ".dango" / "metabase.yml"
    if credentials_file.exists():
        return {"already_configured": True, "success": True}

    # Resolve admin email: env var > auth DB > fallback
    admin_email = os.environ.get("DANGO_ADMIN_EMAIL", "")
    if not admin_email:
        try:
            from dango.auth.admin import get_auth_db_path
            from dango.auth.database import list_users
            from dango.auth.models import Role

            db_path = get_auth_db_path(project_root)
            if db_path.exists():
                users = list_users(db_path, active_only=True)
                admins = [u for u in users if u.role == Role.ADMIN]
                if admins and admins[0].email not in (
                    "admin@dango.local",
                    "admin@localhost",
                ):
                    admin_email = admins[0].email
        except Exception:
            pass
    if not admin_email:
        from dango.logging import get_logger as _get_logger

        _logger = _get_logger(__name__)
        _logger.warning(
            "metabase_setup_skipped",
            reason="No admin email found. Metabase setup will complete on next restart.",
        )
        return {"already_configured": False, "success": True, "skipped": True}

    # Validate email domain — Metabase rejects domains without a dot (e.g. localhost)
    if "@" in admin_email:
        domain = admin_email.split("@")[1]
        if "." not in domain:
            from dango.logging import get_logger as _get_logger2

            _logger2 = _get_logger2(__name__)
            _logger2.warning(
                "metabase_setup_skipped",
                reason=f"Admin email domain invalid for Metabase: {domain}",
            )
            return {"already_configured": False, "success": True, "skipped": True}

    setup_result = setup_metabase(
        project_root,
        project_name,
        admin_email,
        organization=organization,
        cloud_mode=is_cloud_mode(project_root),
    )

    # Link Metabase admin to Dango admin for SSO bridging
    _link_metabase_admin(project_root, admin_email)

    return {"already_configured": False, **setup_result}


def _link_metabase_admin(project_root: Path, admin_email: str) -> None:
    """Link the Metabase admin account to the Dango admin user for SSO."""
    try:
        import requests
        import yaml

        from dango.auth.admin import get_auth_db_path
        from dango.auth.database import get_user_by_email, update_user
        from dango.auth.metabase_sync import (
            encrypt_metabase_password,
            find_metabase_user_by_email,
            generate_metabase_password,
            update_metabase_user_password,
        )
        from dango.auth.models import UserUpdate
        from dango.logging import get_logger

        _logger = get_logger(__name__)

        # Read Metabase credentials
        mb_yml_path = project_root / ".dango" / "metabase.yml"
        if not mb_yml_path.exists():
            return
        with open(mb_yml_path) as f:
            mb_creds = yaml.safe_load(f)
        mb_url = mb_creds.get("metabase_url", "http://localhost:3000").rstrip("/")
        admin_creds = mb_creds.get("admin", {})

        # Get Metabase admin session
        mb_email = admin_creds.get("email")
        mb_password = admin_creds.get("password")
        if not mb_email or not mb_password:
            return
        resp = requests.post(
            f"{mb_url}/api/session",
            json={"username": mb_email, "password": mb_password},
            timeout=10,
        )
        if resp.status_code != 200:
            return
        session_token = resp.json().get("id")
        if not session_token:
            return

        # Find Dango admin user
        db_path = get_auth_db_path(project_root)
        if not db_path.exists():
            return
        dango_user = get_user_by_email(db_path, admin_email)
        if dango_user is None or dango_user.metabase_user_id is not None:
            return  # Already linked or user doesn't exist

        # Find Metabase user by email
        mb_user = find_metabase_user_by_email(mb_url, session_token, admin_email)
        if mb_user is None:
            return

        # Generate new password, update Metabase user, store in Dango.
        # old_password is required by Metabase when the session user is the
        # same as the target user (admin changing their own password).
        password = generate_metabase_password()
        if not update_metabase_user_password(
            mb_url,
            session_token,
            mb_user["id"],
            password,
            old_password=mb_password,
        ):
            _logger.warning(
                "metabase_password_update_failed",
                metabase_user_id=mb_user["id"],
            )
            return
        encrypted_pw = encrypt_metabase_password(password, project_root)
        update_user(
            db_path,
            dango_user.id,
            UserUpdate(metabase_user_id=mb_user["id"], metabase_password_enc=encrypted_pw),
        )
        _logger.info("metabase_admin_linked", email=admin_email, metabase_user_id=mb_user["id"])
    except Exception:
        # Non-critical — SSO bridge will lazy-sync on next login
        pass


def import_dashboards(project_root: Path) -> dict[str, Any] | None:
    """
    Import YAML dashboards if any exist in the dashboards/ directory.

    Args:
        project_root: Project root directory

    Returns:
        Import result dict (with 'imported' and 'skipped' keys), or None if
        no dashboards directory or no .yml files found.
    """
    dashboards_dir = project_root / "dashboards"
    if not dashboards_dir.exists() or not list(dashboards_dir.glob("*.yml")):
        return None

    from dango.visualization.dashboard_manager import import_dashboards as _import_dashboards

    return _import_dashboards(project_root)
