"""dango/visualization/metabase.py

Creates and provisions "Data Pipeline Health" dashboard for monitoring data pipelines. Designed for demo projects and instant value demonstration.
"""

import logging
import os
import secrets
import string
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

logger = logging.getLogger(__name__)


# Dashboard SQL Queries
#
# These queries run against the `_dango_meta` schema of the DuckDB warehouse
# (sync_history, dbt_test_results, source_overview tables), NOT hardcoded
# constants. That schema is populated by
# `dango.utils.pipeline_health.materialize_pipeline_health()` — see that
# module's docstring for why materialization (rather than e.g. mounting the
# underlying JSON state files into Metabase's container) was chosen, and
# `dango/templates/docker-compose.yml.j2` for why the JSON files aren't
# directly reachable from Metabase in the first place (its container only
# mounts `./data:/data:ro`).
#
# 1.0.8-DASH-1: replaced the previous hardcoded placeholder SQL (health
# score always 100/"Excellent", tests always "All Tests Passing", etc. —
# see BUGS-FOUND.md's "Data Pipeline Health" entry) with the queries below.

DASHBOARD_QUERIES = {
    "source_overview": {
        "name": "Data Sources Overview",
        "description": "Overview of all configured data sources",
        "sql": """
        SELECT
            so.source_name,
            so.source_type,
            so.enabled,
            COALESCE(
                (
                    SELECT sh.status
                    FROM _dango_meta.sync_history sh
                    WHERE sh.source_name = so.source_name
                    ORDER BY sh.sync_timestamp DESC
                    LIMIT 1
                ),
                'never synced'
            ) AS status
        FROM _dango_meta.source_overview so
        ORDER BY so.source_name
        """,
        "visualization": "table",
    },
    "sync_history": {
        "name": "Sync History (Last 7 Days)",
        "description": "Source sync activity over the past week",
        "sql": """
        WITH sync_dates AS (
            SELECT
                date_trunc('day', CURRENT_DATE - INTERVAL (n) DAY) as sync_date
            FROM generate_series(0, 6) as t(n)
        )
        SELECT
            sd.sync_date::DATE as date,
            COUNT(sh.source_name) as syncs_completed
        FROM sync_dates sd
        LEFT JOIN _dango_meta.sync_history sh
            ON date_trunc('day', sh.sync_timestamp) = sd.sync_date
            AND sh.status = 'success'
        GROUP BY sd.sync_date
        ORDER BY sd.sync_date DESC
        """,
        "visualization": "line",
    },
    "data_freshness": {
        "name": "Data Freshness by Source",
        "description": "How recent is the data in each source",
        "sql": """
        SELECT
            so.source_name,
            (
                SELECT sh.rows_processed
                FROM _dango_meta.sync_history sh
                WHERE sh.source_name = so.source_name AND sh.status = 'success'
                ORDER BY sh.sync_timestamp DESC
                LIMIT 1
            ) AS last_sync_row_count,
            (
                SELECT MAX(sh2.sync_timestamp)
                FROM _dango_meta.sync_history sh2
                WHERE sh2.source_name = so.source_name AND sh2.status = 'success'
            ) AS last_updated
        FROM _dango_meta.source_overview so
        ORDER BY last_updated DESC NULLS LAST
        """,
        "visualization": "table",
    },
    "row_counts_trend": {
        "name": "Row Counts Over Time",
        "description": "Track data growth across all sources",
        "sql": """
        -- Cumulative (not per-day) total: sync_history.rows_processed is the
        -- count processed *in that sync* (often an incremental delta), not a
        -- running warehouse total, so a running SUM approximates overall
        -- data growth over the window — matching the "Row Counts Over Time"
        -- / area-chart intent better than a per-day bar would.
        WITH dates AS (
            SELECT date_trunc('day', CURRENT_DATE - INTERVAL (n) DAY) as date
            FROM generate_series(0, 29) as t(n)
        ),
        daily_rows AS (
            SELECT
                date_trunc('day', sync_timestamp) as date,
                SUM(rows_processed) as rows_that_day
            FROM _dango_meta.sync_history
            WHERE status = 'success'
            GROUP BY 1
        )
        SELECT
            d.date::DATE as date,
            SUM(COALESCE(dr.rows_that_day, 0)) OVER (
                ORDER BY d.date ROWS UNBOUNDED PRECEDING
            ) as total_rows
        FROM dates d
        LEFT JOIN daily_rows dr ON dr.date = d.date
        ORDER BY d.date
        """,
        "visualization": "area",
    },
    "dbt_test_results": {
        "name": "dbt Test Results",
        "description": "Data quality tests from dbt",
        "sql": """
        WITH latest_run AS (
            SELECT MAX(run_generated_at) as run_generated_at
            FROM _dango_meta.dbt_test_results
        ),
        latest_tests AS (
            SELECT dtr.*
            FROM _dango_meta.dbt_test_results dtr, latest_run lr
            WHERE dtr.run_generated_at = lr.run_generated_at
                AND dtr.passed IS NOT NULL  -- excludes skipped tests
        )
        SELECT
            CASE
                WHEN COUNT(*) = 0 THEN 'No tests run yet'
                WHEN SUM(CASE WHEN NOT passed THEN 1 ELSE 0 END) = 0 THEN 'All Tests Passing'
                ELSE 'Tests Failing'
            END as status,
            COALESCE(SUM(CASE WHEN NOT passed THEN 1 ELSE 0 END), 0) as failed_tests,
            COUNT(*) as total_tests
        FROM latest_tests
        """,
        "visualization": "scalar",
    },
    "pipeline_health_score": {
        "name": "Pipeline Health Score",
        "description": "Overall health of data pipeline (0-100)",
        "sql": """
        -- Health score definition: a 50/50 weighted average of
        --   (a) sync success rate — % of enabled sources whose most recent
        --       sync attempt succeeded (sources that have never synced are
        --       excluded from this rate, not counted as failures), and
        --   (b) dbt test pass rate — % of tests passing in the latest dbt run.
        -- If only one signal has data (e.g. sources synced but dbt has
        -- never run), that signal is used alone. If neither has data
        -- (fresh install), the score is 0 with an honest message instead of
        -- a fake "Excellent" — see BUGS-FOUND.md's "Data Pipeline Health"
        -- entry for why this matters.
        WITH sync_health AS (
            SELECT
                COUNT(*) as total_enabled,
                SUM(CASE WHEN latest_status = 'success' THEN 1 ELSE 0 END) as succeeded
            FROM (
                SELECT
                    so.source_name,
                    (
                        SELECT sh.status
                        FROM _dango_meta.sync_history sh
                        WHERE sh.source_name = so.source_name
                        ORDER BY sh.sync_timestamp DESC
                        LIMIT 1
                    ) as latest_status
                FROM _dango_meta.source_overview so
                WHERE so.enabled
            ) t
            WHERE latest_status IS NOT NULL
        ),
        test_health AS (
            SELECT
                COUNT(*) as total_tests,
                SUM(CASE WHEN passed THEN 1 ELSE 0 END) as passed_tests
            FROM _dango_meta.dbt_test_results dtr, (
                SELECT MAX(run_generated_at) as m FROM _dango_meta.dbt_test_results
            ) lr
            WHERE dtr.run_generated_at = lr.m AND dtr.passed IS NOT NULL
        )
        SELECT
            CASE
                WHEN sh.total_enabled = 0 AND th.total_tests = 0 THEN 0
                WHEN sh.total_enabled > 0 AND th.total_tests > 0 THEN
                    ROUND(
                        0.5 * (sh.succeeded::DOUBLE / sh.total_enabled) * 100
                        + 0.5 * (th.passed_tests::DOUBLE / th.total_tests) * 100
                    )
                WHEN sh.total_enabled > 0 THEN
                    ROUND((sh.succeeded::DOUBLE / sh.total_enabled) * 100)
                ELSE
                    ROUND((th.passed_tests::DOUBLE / th.total_tests) * 100)
            END as health_score,
            CASE
                WHEN sh.total_enabled = 0 AND th.total_tests = 0 THEN 'No data yet'
                WHEN sh.total_enabled = 0 OR sh.succeeded = sh.total_enabled THEN
                    CASE WHEN th.total_tests = 0 OR th.passed_tests = th.total_tests
                         THEN 'Excellent' ELSE 'Needs Attention' END
                WHEN (sh.succeeded::DOUBLE / sh.total_enabled) >= 0.8 THEN 'Good'
                ELSE 'Needs Attention'
            END as status,
            CASE
                WHEN sh.total_enabled = 0 AND th.total_tests = 0
                    THEN 'No sources have synced yet and no dbt tests have run'
                ELSE
                    COALESCE(sh.succeeded, 0) || '/' || COALESCE(sh.total_enabled, 0)
                    || ' sources synced successfully, '
                    || COALESCE(th.passed_tests, 0) || '/' || COALESCE(th.total_tests, 0)
                    || ' dbt tests passing'
            END as message
        FROM sync_health sh, test_health th
        """,
        "visualization": "gauge",
    },
}


def _metabase_login(
    session: requests.Session,
    metabase_url: str,
    email: str,
    password: str,
    timeout: int = 10,
) -> str | None:
    """POST /api/session and return the session id, or None if login failed (non-200).

    Shared by the three simple (single-attempt) login call sites. Does NOT
    raise — callers decide what a failed login means for their own contract
    (return False, raise click.ClickException, etc). setup_metabase() has its
    own multi-path login/retry logic and does not use this helper — see
    BUGS-FOUND.md for why.
    """
    response = session.post(
        f"{metabase_url}/api/session",
        json={"username": email, "password": password},
        timeout=timeout,
    )
    if response.status_code != 200:
        return None
    return response.json().get("id")


class MetabaseProvisioner:
    """
    Provisions Metabase dashboards via API

    Creates "Data Pipeline Health" dashboard with:
    - Source sync status and activity
    - Data freshness indicators
    - Row count trends
    - dbt test results
    - Overall pipeline health score
    """

    def __init__(
        self,
        metabase_url: str = "http://localhost:3000",
        username: str = "",
        password: str = "",
    ):
        """
        Initialize Metabase provisioner

        Args:
            metabase_url: Metabase instance URL
            username: Admin username. Defaults to "" — real callers must supply this
                explicitly; it previously defaulted to "admin@example.com", a
                hardcoded-credential-shaped trap for any future caller (see
                1.0.8-BUGS-FOUND.md)
            password: Admin password. Defaults to "", same reasoning as username
        """
        self.metabase_url = metabase_url.rstrip("/")
        self.username = username
        self.password = password
        self.session_token = None
        self.session = requests.Session()

    def authenticate(self) -> bool:
        """
        Authenticate with Metabase API

        Returns:
            True if authentication successful
        """
        try:
            self.session_token = _metabase_login(
                self.session, self.metabase_url, self.username, self.password
            )
            return bool(self.session_token)

        except Exception as e:
            print(f"Authentication failed: {e}")
            return False

    def get_database_id(self, database_name: str = "DuckDB") -> int | None:
        """
        Get database ID from Metabase

        Args:
            database_name: Name of database

        Returns:
            Database ID or None
        """
        if not self.session_token:
            return None

        try:
            headers = {"X-Metabase-Session": self.session_token}
            response = self.session.get(
                f"{self.metabase_url}/api/database", headers=headers, timeout=10
            )

            if response.status_code == 200:
                databases = response.json().get("data", [])
                for db in databases:
                    if database_name.lower() in db.get("name", "").lower():
                        return db.get("id")

        except Exception as e:
            print(f"Failed to get database ID: {e}")

        return None

    def create_card(
        self, query_key: str, database_id: int, collection_id: int | None = None
    ) -> int | None:
        """
        Create a Metabase card (question) from query definition

        Args:
            query_key: Key in DASHBOARD_QUERIES
            database_id: Metabase database ID
            collection_id: Optional collection ID

        Returns:
            Card ID or None
        """
        if not self.session_token or query_key not in DASHBOARD_QUERIES:
            return None

        query_def = DASHBOARD_QUERIES[query_key]

        card_data = {
            "name": query_def["name"],
            "description": query_def["description"],
            "dataset_query": {
                "type": "native",
                "native": {"query": query_def["sql"]},
                "database": database_id,
            },
            "display": query_def["visualization"],
            "visualization_settings": {},
        }

        if collection_id:
            card_data["collection_id"] = collection_id

        try:
            headers = {"X-Metabase-Session": self.session_token}
            response = self.session.post(
                f"{self.metabase_url}/api/card", headers=headers, json=card_data, timeout=10
            )

            if response.status_code == 200:
                return response.json().get("id")

        except Exception as e:
            print(f"Failed to create card '{query_def['name']}': {e}")

        return None

    def create_dashboard(
        self,
        name: str = "Data Pipeline Health",
        description: str = "Monitor your data pipeline health and sync status",
    ) -> int | None:
        """
        Create empty dashboard

        Args:
            name: Dashboard name
            description: Dashboard description

        Returns:
            Dashboard ID or None
        """
        if not self.session_token:
            return None

        dashboard_data = {"name": name, "description": description}

        try:
            headers = {"X-Metabase-Session": self.session_token}
            response = self.session.post(
                f"{self.metabase_url}/api/dashboard",
                headers=headers,
                json=dashboard_data,
                timeout=10,
            )

            if response.status_code == 200:
                return response.json().get("id")

        except Exception as e:
            print(f"Failed to create dashboard: {e}")

        return None

    def set_dashboard_cards(
        self,
        dashboard_id: int,
        cards: list[dict[str, Any]],
    ) -> bool:
        """
        Attach a set of cards to a dashboard in a single call.

        Metabase 0.62 removed `POST /api/dashboard/:id/cards` (it now 404s).
        Cards are attached via `PUT /api/dashboard/:id` with a full
        `dashcards` array instead -- Metabase replaces the dashboard's whole
        card layout on each PUT, so all cards must be sent together. Each
        new dashcard needs a unique negative placeholder `id`.

        Args:
            dashboard_id: Dashboard ID
            cards: List of dicts, each with card_id, row, col, size_x, size_y

        Returns:
            True if successful

        Raises:
            RuntimeError: if the Metabase API call fails. Raised (rather
                than silently returning False) so a future Metabase API
                change can't hide the same way this one did -- cards
                silently failing to attach while the dashboard reported
                success with zero cards.
        """
        if not self.session_token:
            return False

        dashcards = [
            {
                "id": -(i + 1),  # negative placeholder id required for new dashcards
                "card_id": card["card_id"],
                "row": card["row"],
                "col": card["col"],
                "size_x": card["size_x"],
                "size_y": card["size_y"],
            }
            for i, card in enumerate(cards)
        ]

        headers = {"X-Metabase-Session": self.session_token}
        try:
            response = self.session.put(
                f"{self.metabase_url}/api/dashboard/{dashboard_id}",
                headers=headers,
                json={"dashcards": dashcards},
                timeout=10,
            )
        except Exception as e:
            raise RuntimeError(f"Failed to attach cards to dashboard {dashboard_id}: {e}") from e

        if response.status_code != 200:
            raise RuntimeError(
                f"Failed to attach cards to dashboard {dashboard_id}: "
                f"{response.status_code} {response.text[:500]}"
            )

        return True

    def provision_pipeline_health_dashboard(self, database_id: int | None = None) -> dict[str, Any]:
        """
        Provision complete Data Pipeline Health dashboard

        Args:
            database_id: Known Metabase database ID to use directly, bypassing
                the name-search fallback below. `setup_metabase()` names the
                DuckDB connection ``f"{org_name} Analytics"`` and persists its
                ID in ``.dango/metabase.yml`` — passing that ID here avoids
                relying on `get_database_id()`'s default `"DuckDB"` substring
                search, which never matches that naming convention (found
                2026-09-04: every real project's database name is
                "<org> Analytics", never containing the literal word
                "DuckDB", so the search always failed).

        Returns:
            Summary of provisioning results
        """
        summary = {
            "success": False,
            "dashboard_id": None,
            "dashboard_url": None,
            "cards_created": [],
            "errors": [],
        }

        # Authenticate
        if not self.authenticate():
            summary["errors"].append("Authentication failed")
            return summary

        # Get database ID: prefer the caller-supplied known ID; fall back to
        # the name-search only when no ID was supplied (e.g. a caller that
        # doesn't have access to .dango/metabase.yml).
        if database_id is None:
            database_id = self.get_database_id()
        if not database_id:
            summary["errors"].append("DuckDB database not found in Metabase")
            return summary

        # Create dashboard
        dashboard_id = self.create_dashboard()
        if not dashboard_id:
            summary["errors"].append("Failed to create dashboard")
            return summary

        summary["dashboard_id"] = dashboard_id
        summary["dashboard_url"] = f"{self.metabase_url}/dashboard/{dashboard_id}"

        # Create and add cards in organized layout
        card_layout = [
            # Row 0: Header cards
            ("pipeline_health_score", 0, 0, 6, 4),  # Top left: Health score
            ("dbt_test_results", 0, 6, 6, 4),  # Top middle: Test results
            ("source_overview", 0, 12, 6, 4),  # Top right: Source overview
            # Row 1: Trends
            ("sync_history", 4, 0, 9, 6),  # Left: Sync history chart
            ("row_counts_trend", 4, 9, 9, 6),  # Right: Row counts chart
            # Row 2: Details
            ("data_freshness", 10, 0, 18, 4),  # Full width: Freshness table
        ]

        created_cards: list[dict[str, Any]] = []
        for query_key, row, col, size_x, size_y in card_layout:
            card_id = self.create_card(query_key, database_id)
            if card_id:
                created_cards.append(
                    {
                        "query_key": query_key,
                        "card_id": card_id,
                        "row": row,
                        "col": col,
                        "size_x": size_x,
                        "size_y": size_y,
                    }
                )
            else:
                summary["errors"].append(f"Failed to create card: {query_key}")

        # Attach all successfully-created cards to the dashboard in one call
        # (Metabase's PUT /api/dashboard/:id replaces the whole dashcards
        # array, so this can't be done incrementally per card).
        if created_cards:
            try:
                self.set_dashboard_cards(dashboard_id, created_cards)
                summary["cards_created"] = [
                    {"name": DASHBOARD_QUERIES[c["query_key"]]["name"], "card_id": c["card_id"]}
                    for c in created_cards
                ]
            except RuntimeError as e:
                summary["errors"].append(str(e))

        summary["success"] = len(summary["cards_created"]) > 0

        return summary


def provision_dashboard(
    metabase_url: str = "http://localhost:3000",
    *,
    username: str,
    password: str,
    database_id: int | None = None,
) -> dict[str, Any]:
    """
    Convenience function to provision Data Pipeline Health dashboard

    Args:
        metabase_url: Metabase instance URL
        username: Admin username
        password: Admin password
        database_id: Known Metabase database ID — see
            `MetabaseProvisioner.provision_pipeline_health_dashboard`'s docstring
            for why this should be supplied whenever the caller has it
            (e.g. from `.dango/metabase.yml`).

    Returns:
        Provisioning summary
    """
    provisioner = MetabaseProvisioner(metabase_url, username=username, password=password)
    return provisioner.provision_pipeline_health_dashboard(database_id=database_id)


def generate_secure_password(length: int = 20) -> str:
    """Generate a secure random password"""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return "".join(secrets.choice(alphabet) for _ in range(length))


def wait_for_metabase_ready(metabase_url: str = "http://localhost:3000", timeout: int = 60) -> bool:
    """
    Wait for Metabase to be ready

    Args:
        metabase_url: Metabase URL
        timeout: Timeout in seconds

    Returns:
        True if ready, False if timeout
    """
    session = requests.Session()
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = session.get(f"{metabase_url}/api/health", timeout=5)
            if response.status_code == 200:
                return True
        except Exception:
            logger.debug("metabase_health_poll_error", exc_info=True)
        time.sleep(2)
    return False


def hide_internal_tables(metabase_url: str, headers: dict[str, str], db_id: int) -> dict[str, Any]:
    """
    Hide internal tables from Metabase UI (raw_* schemas, _dlt_* tables).

    Uses Metabase's PUT /api/table/:id with visibility_type="technical"
    to hide tables from normal users while keeping them accessible for advanced queries.

    Args:
        metabase_url: Metabase URL
        headers: Request headers with session token
        db_id: Database ID in Metabase

    Returns:
        Summary of hidden tables
    """
    result = {"hidden_count": 0, "errors": []}
    session = requests.Session()

    try:
        # First, trigger a sync to ensure tables are discovered
        session.post(
            f"{metabase_url}/api/database/{db_id}/sync_schema", headers=headers, timeout=10
        )

        # Wait briefly for sync to start discovering tables
        time.sleep(3)

        # Get all tables from this database
        metadata_response = session.get(
            f"{metabase_url}/api/database/{db_id}/metadata", headers=headers, timeout=30
        )

        if metadata_response.status_code != 200:
            result["errors"].append(
                f"Could not get database metadata: {metadata_response.status_code}"
            )
            return result

        metadata = metadata_response.json()
        tables = metadata.get("tables", [])

        # Hide tables from raw_* schemas or internal dlt tables
        for table in tables:
            table_id = table.get("id")
            table_name = table.get("name", "")
            schema = table.get("schema", "")

            # Skip if already hidden
            if table.get("visibility_type") in ("hidden", "technical"):
                continue

            # Hide if: raw_* schema, _dlt_* table, or metadata tables
            should_hide = (
                schema.startswith("raw_")
                or table_name.startswith("_dlt_")
                or table_name in ("spreadsheet", "spreadsheet_info")
                or schema == "main"  # main schema has dlt internal tables
            )

            if should_hide:
                try:
                    hide_response = session.put(
                        f"{metabase_url}/api/table/{table_id}",
                        headers=headers,
                        json={"visibility_type": "technical"},
                        timeout=10,
                    )
                    if hide_response.status_code == 200:
                        result["hidden_count"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to hide {schema}.{table_name}: {e}")

    except Exception as e:
        result["errors"].append(f"Error hiding tables: {e}")

    return result


def _reset_metabase_volume(project_root: Path) -> bool:
    """Remove stale Metabase Docker volume and restart the container.

    Used when Metabase has existing data from a different project (no
    ``metabase.yml``) and setup cannot proceed with the stale state.

    Returns ``True`` if the reset succeeded, ``False`` otherwise.
    """
    from dango.platform.docker import get_compose_project_name

    compose_name = get_compose_project_name(project_root)
    env = {**os.environ, "COMPOSE_PROJECT_NAME": compose_name}

    try:
        # Stop and remove the metabase container
        subprocess.run(
            ["docker", "compose", "stop", "metabase"],
            cwd=project_root,
            env=env,
            capture_output=True,
            timeout=60,
        )
        subprocess.run(
            ["docker", "compose", "rm", "-f", "metabase"],
            cwd=project_root,
            env=env,
            capture_output=True,
            timeout=30,
        )

        # Remove the volume — this is the critical step
        volume_name = f"{compose_name}_metabase-data"
        rm_result = subprocess.run(
            ["docker", "volume", "rm", volume_name],
            capture_output=True,
            timeout=30,
        )
        if rm_result.returncode != 0:
            logger.warning(
                "metabase_volume_rm_failed",
                volume=volume_name,
                stderr=rm_result.stderr.decode(errors="replace").strip(),
            )
            return False

        # Restart metabase container
        subprocess.run(
            ["docker", "compose", "up", "-d", "metabase"],
            cwd=project_root,
            env=env,
            capture_output=True,
            timeout=120,
        )
        return True
    except Exception:
        logger.debug("metabase_volume_reset_failed", exc_info=True)
        return False


def setup_metabase(
    project_root: Path,
    project_name: str,
    admin_email: str,
    organization: str | None = None,
    metabase_url: str = "http://localhost:3000",
    cloud_mode: bool = False,
) -> dict[str, Any]:
    """
    Auto-setup Metabase on first start

    Creates admin user, connects DuckDB, hides H2, stores credentials.

    Args:
        project_root: Path to Dango project root
        project_name: Project name
        organization: Organization name (optional)
        metabase_url: Metabase URL
        cloud_mode: When True, generate a random admin password instead
            of the default local password. SSO session bridging handles
            user-facing access, so the random password is invisible.
        admin_email: Email address for the Metabase admin user.
            Resolved by caller from DANGO_ADMIN_EMAIL env var or auth DB.

    Returns:
        Setup summary with credentials
    """
    summary = {
        "success": False,
        "admin_created": False,
        "duckdb_connected": False,
        "h2_hidden": False,
        "credentials_saved": False,
        "errors": [],
    }

    metabase_url = metabase_url.rstrip("/")
    project_root / "data" / "warehouse.duckdb"
    credentials_file = project_root / ".dango" / "metabase.yml"
    session = requests.Session()

    # Check if already setup
    if credentials_file.exists():
        summary["errors"].append("Metabase already configured (credentials file exists)")
        return summary

    from dango.platform.docker import get_compose_project_name

    compose_name = get_compose_project_name(project_root)

    # Wait for Metabase to be ready (longer timeout for cloud cold start)
    ready_timeout = 300 if cloud_mode else 60
    print("  ⏳ Waiting for Metabase to be ready...")
    if not wait_for_metabase_ready(metabase_url, timeout=ready_timeout):
        summary["errors"].append(f"Metabase not ready after {ready_timeout} seconds")
        return summary

    print("  ✓ Metabase is ready")

    try:
        # Get setup token
        response = session.get(f"{metabase_url}/api/session/properties", timeout=10)
        if response.status_code != 200:
            summary["errors"].append("Could not get setup token")
            return summary

        properties = response.json()
        setup_token = properties.get("setup-token")

        import secrets as _secrets

        admin_password = _secrets.token_urlsafe(32)
        org_name = organization or project_name

        if not setup_token:
            # Metabase already has admin user (likely from previous init with same volume)
            # Save default credentials and try to continue setup
            print("  ⚠ Metabase already initialized, using default credentials")

            # Try to login with default credentials to verify they work
            try:
                login_response = session.post(
                    f"{metabase_url}/api/session",
                    json={"username": admin_email, "password": admin_password},
                    timeout=10,
                )

                if login_response.status_code == 200:
                    session_token = login_response.json().get("id")
                    print("  ✓ Login successful with default credentials")

                    summary["admin_created"] = True  # Already existed

                    # Set headers for DuckDB connection below
                    headers = {"X-Metabase-Session": session_token}
                    # Credentials will be saved at the end with DuckDB info

                else:
                    # Stale volume from a different project — reset and retry
                    print("  ⚠ Stale Metabase volume detected, resetting...")
                    if _reset_metabase_volume(project_root):
                        print("  ⏳ Waiting for Metabase to restart...")
                        if wait_for_metabase_ready(metabase_url, timeout=120):
                            # Get fresh setup token after reset
                            props_resp = session.get(
                                f"{metabase_url}/api/session/properties", timeout=10
                            )
                            if props_resp.status_code == 200:
                                setup_token = props_resp.json().get("setup-token")
                        if not setup_token:
                            summary["errors"].append(
                                "Metabase volume reset but setup token not available."
                            )
                            return summary
                    else:
                        summary["errors"].append(
                            "Metabase already initialized but default credentials don't work. "
                            f"To reset: docker volume rm {compose_name}_metabase-data && dango start"
                        )
                        return summary

            except Exception as e:
                summary["errors"].append(f"Could not login to existing Metabase: {e}")
                return summary

        if setup_token:
            # Fresh Metabase - create admin user with default credentials
            setup_data = {
                "token": setup_token,
                "user": {
                    "first_name": "Admin",
                    "last_name": "User",
                    "email": admin_email,
                    "password": admin_password,
                    "site_name": f"{org_name} Analytics",
                },
                "database": None,  # We'll add DuckDB separately
                "prefs": {"site_name": f"{org_name} Analytics", "allow_tracking": False},
            }

            response = session.post(f"{metabase_url}/api/setup", json=setup_data, timeout=30)

            if response.status_code != 200:
                # Check if user already exists - fall back to login
                if "user currently exists" in response.text or "first user" in response.text:
                    print("  ⚠ User already exists, attempting login with default credentials...")
                    login_response = session.post(
                        f"{metabase_url}/api/session",
                        json={"username": admin_email, "password": admin_password},
                        timeout=10,
                    )

                    if login_response.status_code == 200:
                        session_token = login_response.json().get("id")
                        print("  ✓ Login successful with default credentials")
                        summary["admin_created"] = True
                        headers = {"X-Metabase-Session": session_token}
                    else:
                        summary["errors"].append(
                            f"Failed to create admin user: {response.text}\n"
                            "And could not login with default credentials.\n"
                            f"To reset: docker volume rm {compose_name}_metabase-data && dango start"
                        )
                        return summary
                else:
                    summary["errors"].append(f"Failed to create admin user: {response.text}")
                    return summary
            else:
                summary["admin_created"] = True
                print(f"  ✓ Created admin user: {admin_email}")

                # Login to get session token
                login_response = session.post(
                    f"{metabase_url}/api/session",
                    json={"username": admin_email, "password": admin_password},
                    timeout=10,
                )

                if login_response.status_code != 200:
                    summary["errors"].append("Could not login after creating admin")
                    return summary

                session_token = login_response.json().get("id")
                headers = {"X-Metabase-Session": session_token}

        # At this point, we have headers with session token from either path

        # Check for existing DuckDB connection to prevent duplicates
        existing_db_id = None
        db_name = f"{org_name} Analytics"

        try:
            db_list = session.get(f"{metabase_url}/api/database", headers=headers, timeout=10)
            if db_list.status_code == 200:
                databases = db_list.json().get("data", [])
                for db in databases:
                    if db.get("engine") == "duckdb" and db.get("name") == db_name:
                        existing_db_id = db.get("id")
                        print(
                            f"  ℹ DuckDB connection already exists (ID: {existing_db_id}), will update it"
                        )
                        break
        except Exception:  # noqa: BLE001
            pass  # If check fails, proceed with creation

        # Add or update DuckDB connection
        # Note: Metabase runs in Docker with data mounted at /data (see docker-compose.yml)
        docker_duckdb_path = "/data/warehouse.duckdb"

        duckdb_config = {
            "name": db_name,
            "engine": "duckdb",
            "details": {
                "database_file": docker_duckdb_path,
                "old_implicit_casting": True,
                "read_only": True,
                # Note: DuckDB driver doesn't support schema-filters-type/patterns
                # Users will see all schemas (raw_*, main, staging)
                # This is a limitation of the DuckDB Metabase driver
            },
        }

        if existing_db_id:
            # Update existing connection
            db_response = session.put(
                f"{metabase_url}/api/database/{existing_db_id}",
                headers=headers,
                json=duckdb_config,
                timeout=10,
            )
        else:
            # Create new connection
            db_response = session.post(
                f"{metabase_url}/api/database", headers=headers, json=duckdb_config, timeout=10
            )

        if db_response.status_code == 200:
            response_data = db_response.json()
            # For updates, use existing_db_id; for creates, get from response
            duckdb_id = existing_db_id or response_data.get("id")

            # Verify we actually got a database ID (not just a 200 response)
            if duckdb_id:
                summary["duckdb_connected"] = True
                summary["duckdb_id"] = duckdb_id
                print(f"  ✓ Connected DuckDB (Database ID: {duckdb_id})")

                # Set as default database
                try:
                    session.put(
                        f"{metabase_url}/api/database/{duckdb_id}",
                        headers=headers,
                        json={"is_sample": False, "is_full_sync": True},
                        timeout=10,
                    )
                except Exception:  # noqa: BLE001
                    pass  # Not critical

                # NOTE: hide_internal_tables disabled - it breaks Metabase schema navigation
                # When tables are marked as "technical", Metabase collapses from schema-based
                # view to flat table list. Users will see all tables including internal ones,
                # but schema organization is preserved.
                # TODO: Find alternative approach that hides tables without breaking schema nav
                # hide_result = hide_internal_tables(metabase_url, headers, duckdb_id)
                # if hide_result["hidden_count"] > 0:
                #     print(f"  ✓ Hidden {hide_result['hidden_count']} internal table(s)")
                #     summary["tables_hidden"] = hide_result["hidden_count"]
            else:
                # Got 200 but no ID - connection validation failed
                error_msg = (
                    response_data.get("message")
                    or response_data.get("errors")
                    or str(response_data)
                )
                summary["errors"].append(f"Failed to connect DuckDB: {error_msg}")
                print(f"  ✗ Failed to connect DuckDB: {error_msg}")
        else:
            # DuckDB connection failed - log the error
            error_detail = (
                db_response.text if db_response.text else f"Status {db_response.status_code}"
            )
            summary["errors"].append(f"Failed to connect DuckDB: {error_detail}")
            print(f"  ✗ Failed to connect DuckDB: {error_detail}")

        # Hide H2 sample database and remove example content
        try:
            # Get all databases to find H2
            db_list_response = session.get(
                f"{metabase_url}/api/database", headers=headers, timeout=10
            )

            if db_list_response.status_code == 200:
                databases = db_list_response.json().get("data", [])
                for db in databases:
                    db_id = db.get("id")
                    db_engine = db.get("engine")

                    # Hide H2 databases
                    if db_engine == "h2":
                        try:
                            # Try to delete it entirely
                            delete_response = session.delete(
                                f"{metabase_url}/api/database/{db_id}", headers=headers, timeout=10
                            )
                            if delete_response.status_code == 204:
                                summary["h2_hidden"] = True
                                print(f"  ✓ Deleted H2 sample database (ID: {db_id})")
                            else:
                                # If delete fails, try to hide it
                                hide_response = session.put(
                                    f"{metabase_url}/api/database/{db_id}",
                                    headers=headers,
                                    json={"is_sample": True},
                                    timeout=10,
                                )
                                if hide_response.status_code == 200:
                                    summary["h2_hidden"] = True
                                    print(f"  ✓ Hidden H2 sample database (ID: {db_id})")
                        except Exception:  # noqa: BLE001
                            pass
        except Exception:  # noqa: BLE001
            pass  # Not critical

        # Remove example dashboards and collections
        try:
            # Get all collections
            collections_response = session.get(
                f"{metabase_url}/api/collection", headers=headers, timeout=10
            )

            if collections_response.status_code == 200:
                collections = collections_response.json()
                for collection in collections:
                    collection_id = collection.get("id")
                    collection_name = collection.get("name", "").lower()

                    # Skip our created collections and root collection
                    if collection_name in ["shared", "personal"] or collection_id == "root":
                        continue

                    # Archive example collections
                    try:
                        archive_response = session.put(
                            f"{metabase_url}/api/collection/{collection_id}",
                            headers=headers,
                            json={"archived": True},
                            timeout=10,
                        )
                        if archive_response.status_code == 200:
                            print(f"  ✓ Archived example collection: {collection.get('name')}")
                    except Exception:
                        pass
        except Exception:
            pass  # Not critical

        # Create "Shared" and "Personal" collections
        collections_created = []
        for collection_name, description in [
            ("Shared", "Dashboards shared with the team (exported to git)"),
            ("Personal", "Personal dashboards and experiments (not exported)"),
        ]:
            try:
                collection_data = {
                    "name": collection_name,
                    "description": description,
                    "color": "#509EE3" if collection_name == "Shared" else "#9AA0AF",
                }
                coll_response = session.post(
                    f"{metabase_url}/api/collection",
                    headers=headers,
                    json=collection_data,
                    timeout=10,
                )
                if coll_response.status_code == 200:
                    collections_created.append(collection_name)
                    print(f"  ✓ Created '{collection_name}' collection")
            except Exception:  # noqa: BLE001
                pass  # Not critical

        summary["collections_created"] = collections_created

        # CRITICAL: Only save credentials if DuckDB connection succeeded
        # Without DuckDB, Metabase is unusable - don't claim success
        if not summary.get("duckdb_connected"):
            # DuckDB connection failed - don't save credentials
            # This allows setup to retry on next `dango start`
            print("  ✗ Skipping credentials save (DuckDB connection required)")
            summary["success"] = False
            return summary

        # Save credentials to .dango/metabase.yml (gitignored)
        credentials = {
            "metabase_url": metabase_url,
            "admin": {"email": admin_email, "password": admin_password},
            "database": {"id": summary.get("duckdb_id"), "name": f"{org_name} Analytics"},
            "setup_completed_at": datetime.now(tz=timezone.utc).isoformat(),
        }

        credentials_file.parent.mkdir(parents=True, exist_ok=True)
        with open(credentials_file, "w") as f:
            yaml.safe_dump(credentials, f, default_flow_style=False)

        summary["credentials_saved"] = True
        summary["credentials_file"] = str(credentials_file)
        print(f"  ✓ Saved credentials to {credentials_file}")

        summary["success"] = True
        summary["admin_email"] = admin_email
        summary["metabase_url"] = f"{metabase_url}"

    except Exception as e:
        summary["errors"].append(f"Setup error: {str(e)}")

    return summary


def sync_metabase_schema(project_root: Path, metabase_url: str = "http://localhost:3000") -> bool:
    """
    Trigger Metabase to re-sync database schema (table/column metadata).

    This is a lightweight operation that just queries information_schema
    to update Metabase's internal metadata cache.

    Args:
        project_root: Path to project root
        metabase_url: Metabase URL (default: http://localhost:3000)

    Returns:
        True if sync triggered successfully, False otherwise
    """
    import yaml

    credentials_file = project_root / ".dango" / "metabase.yml"

    # Check if Metabase is configured
    if not credentials_file.exists():
        return False

    session = requests.Session()

    try:
        # Load credentials
        with open(credentials_file) as f:
            credentials = yaml.safe_load(f)

        # Get database ID from nested structure
        database_id = credentials.get("database", {}).get("id")
        if not database_id:
            return False

        # Get admin credentials
        admin = credentials.get("admin", {})
        email = admin.get("email")
        password = admin.get("password")

        if not email or not password:
            return False

        # Login to get session
        session_id = _metabase_login(session, metabase_url, email, password)
        if not session_id:
            return False

        # Trigger sync
        response = session.post(
            f"{metabase_url}/api/database/{database_id}/sync_schema",
            headers={"X-Metabase-Session": session_id},
            timeout=10,
        )

        if response.status_code != 200:
            return False

        # Wait for sync to complete (poll up to 30 seconds)
        import time

        for _ in range(30):
            time.sleep(1)
            db_status = session.get(
                f"{metabase_url}/api/database/{database_id}",
                headers={"X-Metabase-Session": session_id},
                timeout=5,
            )
            if db_status.status_code == 200:
                # Check if sync is complete (no longer has 'initial_sync_status')
                db_data = db_status.json()
                if (
                    not db_data.get("initial_sync_status")
                    or db_data.get("initial_sync_status") == "complete"
                ):
                    break

        # Update table descriptions to guide users
        tables: list[dict[str, Any]] = []
        try:
            # Get all tables
            metadata_response = session.get(
                f"{metabase_url}/api/database/{database_id}/metadata",
                headers={"X-Metabase-Session": session_id},
                timeout=10,
            )

            if metadata_response.status_code == 200:
                tables = metadata_response.json().get("tables", [])

                for table in tables:
                    schema = table.get("schema")
                    table_id = table.get("id")
                    table_name = table.get("name")

                    # Set description and visibility based on schema
                    visibility_type = None  # Normal visibility by default

                    # Hide dlt internal staging schemas (e.g., raw_source_staging)
                    if schema and schema.endswith("_staging"):
                        description = "⚙️ **DLT INTERNAL** - Temporary staging data (do not use)"
                        visibility_type = "hidden"
                    elif schema == "raw" or (schema and schema.startswith("raw_")):
                        description = (
                            "⚠️ **RAW SOURCE DATA** - Do not use for dashboards\n\n"
                            "This is unprocessed data exactly as loaded from the source. "
                            f"Use `staging.stg_{table_name}` instead for analysis and visualizations."
                        )
                        visibility_type = "hidden"  # Hide from UI, still SQL-queryable
                    elif schema == "staging":
                        description = (
                            "✅ **READY FOR ANALYSIS**\n\n"
                            "Clean, typed data ready for dashboards and reports. "
                            "This is the recommended table for building visualizations."
                        )
                    elif schema == "intermediate":
                        description = (
                            "🔄 **INTERMEDIATE MODELS**\n\n"
                            "Reusable business logic and transformations. "
                            "Building blocks for marts. Not intended for direct analysis - use marts instead."
                        )
                    elif schema == "marts":
                        description = (
                            "📈 **BUSINESS METRICS**\n\n"
                            "Pre-built metrics and aggregates for common business questions. "
                            "Optimized for dashboard performance."
                        )
                    elif schema == "main" and table_name.startswith("_dango"):
                        description = "⚙️ **INTERNAL** - Dango metadata (do not use)"
                        visibility_type = "hidden"
                    else:
                        continue  # Skip tables without clear guidance

                    # Update table description and visibility
                    update_payload = {"description": description}
                    if visibility_type:
                        update_payload["visibility_type"] = visibility_type

                    response = session.put(
                        f"{metabase_url}/api/table/{table_id}",
                        headers={"X-Metabase-Session": session_id},
                        json=update_payload,
                        timeout=5,
                    )
                    if response.status_code != 200:
                        logger.warning(
                            f"Failed to update table {schema}.{table_name} (id={table_id}): "
                            f"status={response.status_code}, response={response.text[:200]}"
                        )

        except Exception as e:
            # Log but don't fail - descriptions are nice-to-have
            logger.warning(f"Error updating table metadata: {e}")

        if not tables:
            logger.warning(
                "sync_metabase_schema: no tables found after sync poll — "
                "Metabase may still be syncing. database_id=%s",
                database_id,
            )
            return False

        return True

    except Exception:
        # Silent failure - don't block sync if Metabase isn't running
        return False


def set_metabase_telemetry(
    project_root: Path, enabled: bool, metabase_url: str | None = None
) -> None:
    """
    Toggle Metabase's anonymous usage tracking via the admin Setting API.

    Loads admin credentials from .dango/metabase.yml (same pattern as
    sync_metabase_schema), logs in, then calls
    PUT /api/setting/anon-tracking-enabled — Metabase's runtime setting key
    for anonymous tracking (distinct from the one-time "allow_tracking" field
    used only in the /api/setup wizard payload in setup_metabase() above).

    On success, also writes a local last-known-state cache
    (.dango/metabase_telemetry_state) so `dango telemetry status` can report
    the real state without making a live API call every time — see
    dango/cli/commands/telemetry.py's `_get_metabase_telemetry_state()`.

    Args:
        project_root: Path to project root
        enabled: True to enable anonymous tracking, False to disable it
        metabase_url: Metabase URL. If not given, read from the
            "metabase_url" key in .dango/metabase.yml (same precedent as
            cli/commands/metabase_cmd.py), falling back to
            http://localhost:3000 if that key is absent too.

    Raises:
        click.ClickException: If Metabase credentials are missing/incomplete,
            or if the API call fails (e.g. Metabase not running), or if
            anything else in the credentials/login/API flow goes wrong.
    """
    import click

    credentials_file = project_root / ".dango" / "metabase.yml"
    if not credentials_file.exists():
        raise click.ClickException("Metabase not configured. Run dango start first.")

    try:
        with open(credentials_file) as f:
            credentials = yaml.safe_load(f) or {}

        admin = credentials.get("admin", {})
        email = admin.get("email")
        password = admin.get("password")
        if not email or not password:
            raise click.ClickException(
                "Metabase admin credentials missing from .dango/metabase.yml"
            )

        resolved_url = metabase_url or credentials.get("metabase_url", "http://localhost:3000")

        session = requests.Session()
        login_response = session.post(
            f"{resolved_url}/api/session",
            json={"username": email, "password": password},
            timeout=10,
        )
        if login_response.status_code in (401, 403):
            # Distinguish "reachable but rejected the credentials" from
            # "unreachable" *before* raise_for_status() below would
            # otherwise turn this into a generic requests.HTTPError (a
            # RequestException subclass) and get mislabeled by the
            # "is it running?" branch further down — Metabase is running
            # fine here, the admin password in metabase.yml is just stale.
            raise click.ClickException(
                "Metabase login failed — check admin credentials in .dango/metabase.yml"
            )
        login_response.raise_for_status()
        session_id = login_response.json().get("id")
        if not session_id:
            raise click.ClickException("Metabase login did not return a session id")

        response = session.put(
            f"{resolved_url}/api/setting/anon-tracking-enabled",
            headers={"X-Metabase-Session": session_id},
            json={"value": enabled},
            timeout=10,
        )
        response.raise_for_status()

        # The real API call above already succeeded — enabled is now the
        # actual live Metabase state. A failure writing the local status
        # cache (disk full, permissions, read-only filesystem) is a
        # "dango telemetry status may show a stale value" problem, not a
        # "this command failed" problem, so it's caught and logged here,
        # inside its own try, rather than left to fall into the broad
        # `except Exception` below — that would misreport a successful
        # toggle as a failure just because a secondary, best-effort write
        # didn't land.
        try:
            state_file = project_root / ".dango" / "metabase_telemetry_state"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("true" if enabled else "false")
        except Exception:
            logger.warning(
                "Metabase telemetry set to %s via API, but failed to write "
                "local status cache at %s — `dango telemetry status` may "
                "show a stale value until the next successful toggle.",
                enabled,
                project_root / ".dango" / "metabase_telemetry_state",
                exc_info=True,
            )

    except click.ClickException:
        raise
    except requests.exceptions.RequestException as e:
        raise click.ClickException(
            f"Could not reach Metabase at {resolved_url} — is it running? ({e})"
        ) from e
    except Exception as e:
        # Broad fallback: credentials-load/login/API flow can also fail on
        # yaml.YAMLError (malformed metabase.yml) or a non-JSON 200 login
        # response (login_response.json() raising ValueError), neither of
        # which is a RequestException. Convert to the same clean-error
        # contract this function promises for every other failure mode.
        raise click.ClickException(f"Failed to set Metabase telemetry: {e}") from e


def get_metabase_telemetry_state(project_root: Path | None) -> bool:
    """Return Metabase's last-known opt-in state.

    Reads the local cache file `set_metabase_telemetry()` writes above after
    each successful live API call — this reports the real last-set state
    without requiring Metabase to be running just to print a status table.
    If telemetry was never toggled through this command (no cache file, or
    no project), defaults to "on": that's Metabase's own out-of-the-box
    default for anon-tracking-enabled.

    Relocated here (Level 2, same level as `web/`) from
    `cli/commands/telemetry.py`'s `_get_metabase_telemetry_state()`
    (1.0.8-U) — both the CLI and `web/routes/telemetry.py` call this same
    function so there is one real implementation, not two.
    """
    if project_root is None:
        return True
    state_file = project_root / ".dango" / "metabase_telemetry_state"
    if not state_file.exists():
        return True
    return state_file.read_text().strip() == "true"


def refresh_metabase_connection(
    project_root: Path, metabase_url: str = "http://localhost:3000"
) -> tuple[bool, str | None]:
    """
    Force Metabase to refresh its DuckDB connection to see latest data.

    This is needed because DuckDB connections hold a snapshot of the database.
    After loading new data, Metabase needs to restart to see the changes.

    Args:
        project_root: Path to project root
        metabase_url: Metabase URL

    Returns:
        Tuple of (success, error_message). error_message is None on success.
    """
    import subprocess

    session = requests.Session()

    try:
        # Get container name from DockerManager (uses hash-based naming)
        from dango.platform.docker import DockerManager

        dm = DockerManager(project_root)
        container_name = f"{dm.compose_project_name}-metabase-1"

        # Check if container exists and is running
        check_result = subprocess.run(
            ["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if container_name not in check_result.stdout:
            # Container not running
            return (False, "Metabase container not running")

        # Restart Metabase container to force reconnection
        restart_result = subprocess.run(
            ["docker", "restart", container_name], capture_output=True, text=True, timeout=30
        )

        if restart_result.returncode != 0:
            return (False, f"Docker restart failed: {restart_result.stderr[:200]}")

        # Wait for Metabase to come back up (max 20 seconds)
        max_attempts = 20
        for _ in range(max_attempts):
            try:
                response = session.get(f"{metabase_url}/api/health", timeout=1)
                if response.status_code == 200:
                    return (True, None)
            except requests.exceptions.RequestException:  # noqa: BLE001
                pass
            time.sleep(1)

        return (False, "Metabase did not become healthy after restart")

    except Exception as e:
        logger.warning("refresh_metabase_connection_error", error=str(e), exc_info=True)
        return (False, str(e))
