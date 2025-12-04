"""
Metabase Dashboard Provisioning

Creates and provisions "Data Pipeline Health" dashboard for monitoring data pipelines.
Designed for demo projects and instant value demonstration.

Auto-setup functionality (MVP):
- Auto-create admin user on first start
- Auto-connect to DuckDB
- Hide H2 sample database
- Store credentials in .dango/metabase.yml (gitignored)
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import logging
import requests
from datetime import datetime
import secrets
import string
import yaml
import time

logger = logging.getLogger(__name__)


# Dashboard SQL Queries
# These queries work against DuckDB with dlt state tables

DASHBOARD_QUERIES = {
    "source_overview": {
        "name": "Data Sources Overview",
        "description": "Overview of all configured data sources",
        "sql": """
        SELECT
            name as source_name,
            type as source_type,
            enabled,
            'Synced' as status  -- Placeholder, will be enhanced with actual state
        FROM (VALUES
            ('sample', 'csv', true),
            ('demo', 'csv', true)
        ) as t(name, type, enabled)
        -- TODO: Replace with actual sources.yml data
        """,
        "visualization": "table"
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
            sync_date::DATE as date,
            0 as syncs_completed  -- Placeholder
        FROM sync_dates
        ORDER BY sync_date DESC
        """,
        "visualization": "line"
    },

    "data_freshness": {
        "name": "Data Freshness by Source",
        "description": "How recent is the data in each source",
        "sql": """
        SELECT
            'sample_data' as source_name,
            COUNT(*) as row_count,
            MAX(CURRENT_TIMESTAMP) as last_updated
        FROM (SELECT 1)  -- Placeholder
        -- TODO: Query actual staging tables
        """,
        "visualization": "table"
    },

    "row_counts_trend": {
        "name": "Row Counts Over Time",
        "description": "Track data growth across all sources",
        "sql": """
        WITH dates AS (
            SELECT date_trunc('day', CURRENT_DATE - INTERVAL (n) DAY) as date
            FROM generate_series(0, 29) as t(n)
        )
        SELECT
            date::DATE,
            0 as total_rows  -- Placeholder
        FROM dates
        ORDER BY date
        """,
        "visualization": "area"
    },

    "dbt_test_results": {
        "name": "dbt Test Results",
        "description": "Data quality tests from dbt",
        "sql": """
        SELECT
            'All Tests Passing' as status,
            0 as failed_tests,
            0 as total_tests
        -- TODO: Parse dbt test results
        """,
        "visualization": "scalar"
    },

    "pipeline_health_score": {
        "name": "Pipeline Health Score",
        "description": "Overall health of data pipeline (0-100)",
        "sql": """
        SELECT
            100 as health_score,
            'Excellent' as status,
            'All sources syncing successfully' as message
        """,
        "visualization": "gauge"
    }
}


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
        username: str = "admin@example.com",
        password: str = "admin123"
    ):
        """
        Initialize Metabase provisioner

        Args:
            metabase_url: Metabase instance URL
            username: Admin username
            password: Admin password
        """
        self.metabase_url = metabase_url.rstrip('/')
        self.username = username
        self.password = password
        self.session_token = None

    def authenticate(self) -> bool:
        """
        Authenticate with Metabase API

        Returns:
            True if authentication successful
        """
        try:
            response = requests.post(
                f"{self.metabase_url}/api/session",
                json={
                    "username": self.username,
                    "password": self.password
                },
                timeout=10
            )

            if response.status_code == 200:
                self.session_token = response.json().get("id")
                return True
            else:
                return False

        except Exception as e:
            print(f"Authentication failed: {e}")
            return False

    def get_database_id(self, database_name: str = "DuckDB") -> Optional[int]:
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
            response = requests.get(
                f"{self.metabase_url}/api/database",
                headers=headers,
                timeout=10
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
        self,
        query_key: str,
        database_id: int,
        collection_id: Optional[int] = None
    ) -> Optional[int]:
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
                "native": {
                    "query": query_def["sql"]
                },
                "database": database_id
            },
            "display": query_def["visualization"],
            "visualization_settings": {}
        }

        if collection_id:
            card_data["collection_id"] = collection_id

        try:
            headers = {"X-Metabase-Session": self.session_token}
            response = requests.post(
                f"{self.metabase_url}/api/card",
                headers=headers,
                json=card_data,
                timeout=10
            )

            if response.status_code == 200:
                return response.json().get("id")

        except Exception as e:
            print(f"Failed to create card '{query_def['name']}': {e}")

        return None

    def create_dashboard(
        self,
        name: str = "Data Pipeline Health",
        description: str = "Monitor your data pipeline health and sync status"
    ) -> Optional[int]:
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

        dashboard_data = {
            "name": name,
            "description": description
        }

        try:
            headers = {"X-Metabase-Session": self.session_token}
            response = requests.post(
                f"{self.metabase_url}/api/dashboard",
                headers=headers,
                json=dashboard_data,
                timeout=10
            )

            if response.status_code == 200:
                return response.json().get("id")

        except Exception as e:
            print(f"Failed to create dashboard: {e}")

        return None

    def add_card_to_dashboard(
        self,
        dashboard_id: int,
        card_id: int,
        row: int = 0,
        col: int = 0,
        size_x: int = 6,
        size_y: int = 4
    ) -> bool:
        """
        Add card to dashboard with positioning

        Args:
            dashboard_id: Dashboard ID
            card_id: Card ID to add
            row: Row position (0-indexed)
            col: Column position (0-indexed)
            size_x: Width in grid units (0-18)
            size_y: Height in grid units

        Returns:
            True if successful
        """
        if not self.session_token:
            return False

        card_data = {
            "cardId": card_id,
            "row": row,
            "col": col,
            "sizeX": size_x,
            "sizeY": size_y
        }

        try:
            headers = {"X-Metabase-Session": self.session_token}
            response = requests.post(
                f"{self.metabase_url}/api/dashboard/{dashboard_id}/cards",
                headers=headers,
                json=card_data,
                timeout=10
            )

            return response.status_code == 200

        except Exception as e:
            print(f"Failed to add card to dashboard: {e}")
            return False

    def provision_pipeline_health_dashboard(self) -> Dict[str, Any]:
        """
        Provision complete Data Pipeline Health dashboard

        Returns:
            Summary of provisioning results
        """
        summary = {
            "success": False,
            "dashboard_id": None,
            "dashboard_url": None,
            "cards_created": [],
            "errors": []
        }

        # Authenticate
        if not self.authenticate():
            summary["errors"].append("Authentication failed")
            return summary

        # Get database ID
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
            ("dbt_test_results", 0, 6, 6, 4),       # Top middle: Test results
            ("source_overview", 0, 12, 6, 4),       # Top right: Source overview

            # Row 1: Trends
            ("sync_history", 4, 0, 9, 6),           # Left: Sync history chart
            ("row_counts_trend", 4, 9, 9, 6),       # Right: Row counts chart

            # Row 2: Details
            ("data_freshness", 10, 0, 18, 4),       # Full width: Freshness table
        ]

        for query_key, row, col, size_x, size_y in card_layout:
            card_id = self.create_card(query_key, database_id)
            if card_id:
                if self.add_card_to_dashboard(dashboard_id, card_id, row, col, size_x, size_y):
                    summary["cards_created"].append({
                        "name": DASHBOARD_QUERIES[query_key]["name"],
                        "card_id": card_id
                    })
                else:
                    summary["errors"].append(f"Failed to add card: {query_key}")
            else:
                summary["errors"].append(f"Failed to create card: {query_key}")

        summary["success"] = len(summary["cards_created"]) > 0

        return summary


def provision_dashboard(
    metabase_url: str = "http://localhost:3000",
    username: str = "admin@example.com",
    password: str = "admin123"
) -> Dict[str, Any]:
    """
    Convenience function to provision Data Pipeline Health dashboard

    Args:
        metabase_url: Metabase instance URL
        username: Admin username
        password: Admin password

    Returns:
        Provisioning summary
    """
    provisioner = MetabaseProvisioner(metabase_url, username, password)
    return provisioner.provision_pipeline_health_dashboard()


def create_pipeline_health_dashboard(project_root: Path) -> Dict[str, Any]:
    """
    Create Data Pipeline Health dashboard for a Dango project

    Args:
        project_root: Path to Dango project root

    Returns:
        Provisioning summary
    """
    # Read Metabase credentials from project config if available
    # For now, use defaults
    return provision_dashboard()


def generate_secure_password(length: int = 20) -> str:
    """Generate a secure random password"""
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def wait_for_metabase_ready(metabase_url: str = "http://localhost:3000", timeout: int = 60) -> bool:
    """
    Wait for Metabase to be ready

    Args:
        metabase_url: Metabase URL
        timeout: Timeout in seconds

    Returns:
        True if ready, False if timeout
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{metabase_url}/api/health", timeout=5)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def hide_internal_tables(
    metabase_url: str,
    headers: Dict[str, str],
    db_id: int
) -> Dict[str, Any]:
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

    try:
        # First, trigger a sync to ensure tables are discovered
        requests.post(
            f"{metabase_url}/api/database/{db_id}/sync_schema",
            headers=headers,
            timeout=10
        )

        # Wait briefly for sync to start discovering tables
        time.sleep(3)

        # Get all tables from this database
        metadata_response = requests.get(
            f"{metabase_url}/api/database/{db_id}/metadata",
            headers=headers,
            timeout=30
        )

        if metadata_response.status_code != 200:
            result["errors"].append(f"Could not get database metadata: {metadata_response.status_code}")
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
                schema.startswith("raw_") or
                table_name.startswith("_dlt_") or
                table_name in ("spreadsheet", "spreadsheet_info") or
                schema == "main"  # main schema has dlt internal tables
            )

            if should_hide:
                try:
                    hide_response = requests.put(
                        f"{metabase_url}/api/table/{table_id}",
                        headers=headers,
                        json={"visibility_type": "technical"},
                        timeout=10
                    )
                    if hide_response.status_code == 200:
                        result["hidden_count"] += 1
                except Exception as e:
                    result["errors"].append(f"Failed to hide {schema}.{table_name}: {e}")

    except Exception as e:
        result["errors"].append(f"Error hiding tables: {e}")

    return result


def setup_metabase(
    project_root: Path,
    project_name: str,
    organization: Optional[str] = None,
    metabase_url: str = "http://localhost:3000"
) -> Dict[str, Any]:
    """
    Auto-setup Metabase on first start

    Creates admin user, connects DuckDB, hides H2, stores credentials.

    Args:
        project_root: Path to Dango project root
        project_name: Project name
        organization: Organization name (optional)
        metabase_url: Metabase URL

    Returns:
        Setup summary with credentials
    """
    summary = {
        "success": False,
        "admin_created": False,
        "duckdb_connected": False,
        "h2_hidden": False,
        "credentials_saved": False,
        "errors": []
    }

    metabase_url = metabase_url.rstrip('/')
    duckdb_path = project_root / "data" / "warehouse.duckdb"
    credentials_file = project_root / ".dango" / "metabase.yml"

    # Check if already setup
    if credentials_file.exists():
        summary["errors"].append("Metabase already configured (credentials file exists)")
        return summary

    # Wait for Metabase to be ready
    print("  ⏳ Waiting for Metabase to be ready...")
    if not wait_for_metabase_ready(metabase_url):
        summary["errors"].append("Metabase not ready after 60 seconds")
        return summary

    print("  ✓ Metabase is ready")

    try:
        # Get setup token
        response = requests.get(f"{metabase_url}/api/session/properties", timeout=10)
        if response.status_code != 200:
            summary["errors"].append("Could not get setup token")
            return summary

        properties = response.json()
        setup_token = properties.get("setup-token")

        # Use default credentials for all Dango installations
        # This allows auto-login to work even if Metabase volume persists
        admin_email = "admin@dango.local"
        admin_password = "dangolocal123"
        org_name = organization or project_name

        if not setup_token:
            # Metabase already has admin user (likely from previous init with same volume)
            # Save default credentials and try to continue setup
            print("  ⚠ Metabase already initialized, using default credentials")

            # Try to login with default credentials to verify they work
            try:
                login_response = requests.post(
                    f"{metabase_url}/api/session",
                    json={"username": admin_email, "password": admin_password},
                    timeout=10
                )

                if login_response.status_code == 200:
                    session_token = login_response.json().get("id")
                    print("  ✓ Login successful with default credentials")

                    summary["admin_created"] = True  # Already existed

                    # Set headers for DuckDB connection below
                    headers = {"X-Metabase-Session": session_token}
                    # Credentials will be saved at the end with DuckDB info

                else:
                    summary["errors"].append(
                        "Metabase already initialized but default credentials don't work. "
                        "Delete Docker volume or metabase.yml to reset."
                    )
                    return summary

            except Exception as e:
                summary["errors"].append(f"Could not login to existing Metabase: {e}")
                return summary

        else:
            # Fresh Metabase - create admin user with default credentials
            setup_data = {
                "token": setup_token,
                "user": {
                    "first_name": "Admin",
                    "last_name": "User",
                    "email": admin_email,
                    "password": admin_password,
                    "site_name": f"{org_name} Analytics"
                },
                "database": None,  # We'll add DuckDB separately
                "prefs": {
                    "site_name": f"{org_name} Analytics",
                    "allow_tracking": False
                }
            }

            response = requests.post(
                f"{metabase_url}/api/setup",
                json=setup_data,
                timeout=30
            )

            if response.status_code != 200:
                # Check if user already exists - fall back to login
                if "user currently exists" in response.text or "first user" in response.text:
                    print("  ⚠ User already exists, attempting login with default credentials...")
                    login_response = requests.post(
                        f"{metabase_url}/api/session",
                        json={"username": admin_email, "password": admin_password},
                        timeout=10
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
                            "To reset: docker volume rm <project>_metabase_data && dango start"
                        )
                        return summary
                else:
                    summary["errors"].append(f"Failed to create admin user: {response.text}")
                    return summary
            else:
                summary["admin_created"] = True
                print(f"  ✓ Created admin user: {admin_email}")

                # Login to get session token
                login_response = requests.post(
                    f"{metabase_url}/api/session",
                    json={"username": admin_email, "password": admin_password},
                    timeout=10
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
            db_list = requests.get(
                f"{metabase_url}/api/database",
                headers=headers,
                timeout=10
            )
            if db_list.status_code == 200:
                databases = db_list.json().get("data", [])
                for db in databases:
                    if db.get("engine") == "duckdb" and db.get("name") == db_name:
                        existing_db_id = db.get("id")
                        print(f"  ℹ DuckDB connection already exists (ID: {existing_db_id}), will update it")
                        break
        except Exception:
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
                "read_only": False
                # Note: DuckDB driver doesn't support schema-filters-type/patterns
                # Users will see all schemas (raw_*, main, staging)
                # This is a limitation of the DuckDB Metabase driver
            }
        }

        if existing_db_id:
            # Update existing connection
            db_response = requests.put(
                f"{metabase_url}/api/database/{existing_db_id}",
                headers=headers,
                json=duckdb_config,
                timeout=10
            )
        else:
            # Create new connection
            db_response = requests.post(
                f"{metabase_url}/api/database",
                headers=headers,
                json=duckdb_config,
                timeout=10
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
                    requests.put(
                        f"{metabase_url}/api/database/{duckdb_id}",
                        headers=headers,
                        json={"is_sample": False, "is_full_sync": True},
                        timeout=10
                    )
                except Exception:
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
                error_msg = response_data.get("message") or response_data.get("errors") or str(response_data)
                summary["errors"].append(f"Failed to connect DuckDB: {error_msg}")
                print(f"  ✗ Failed to connect DuckDB: {error_msg}")
        else:
            # DuckDB connection failed - log the error
            error_detail = db_response.text if db_response.text else f"Status {db_response.status_code}"
            summary["errors"].append(f"Failed to connect DuckDB: {error_detail}")
            print(f"  ✗ Failed to connect DuckDB: {error_detail}")

        # Hide H2 sample database and remove example content
        try:
            # Get all databases to find H2
            db_list_response = requests.get(
                f"{metabase_url}/api/database",
                headers=headers,
                timeout=10
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
                            delete_response = requests.delete(
                                f"{metabase_url}/api/database/{db_id}",
                                headers=headers,
                                timeout=10
                            )
                            if delete_response.status_code == 204:
                                summary["h2_hidden"] = True
                                print(f"  ✓ Deleted H2 sample database (ID: {db_id})")
                            else:
                                # If delete fails, try to hide it
                                hide_response = requests.put(
                                    f"{metabase_url}/api/database/{db_id}",
                                    headers=headers,
                                    json={"is_sample": True},
                                    timeout=10
                                )
                                if hide_response.status_code == 200:
                                    summary["h2_hidden"] = True
                                    print(f"  ✓ Hidden H2 sample database (ID: {db_id})")
                        except Exception:
                            pass
        except Exception:
            pass  # Not critical

        # Remove example dashboards and collections
        try:
            # Get all collections
            collections_response = requests.get(
                f"{metabase_url}/api/collection",
                headers=headers,
                timeout=10
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
                        archive_response = requests.put(
                            f"{metabase_url}/api/collection/{collection_id}",
                            headers=headers,
                            json={"archived": True},
                            timeout=10
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
            ("Personal", "Personal dashboards and experiments (not exported)")
        ]:
            try:
                collection_data = {
                    "name": collection_name,
                    "description": description,
                    "color": "#509EE3" if collection_name == "Shared" else "#9AA0AF"
                }
                coll_response = requests.post(
                    f"{metabase_url}/api/collection",
                    headers=headers,
                    json=collection_data,
                    timeout=10
                )
                if coll_response.status_code == 200:
                    collections_created.append(collection_name)
                    print(f"  ✓ Created '{collection_name}' collection")
            except Exception:
                pass  # Not critical

        summary["collections_created"] = collections_created

        # CRITICAL: Only save credentials if DuckDB connection succeeded
        # Without DuckDB, Metabase is unusable - don't claim success
        if not summary.get("duckdb_connected"):
            # DuckDB connection failed - don't save credentials
            # This allows setup to retry on next `dango start`
            print(f"  ✗ Skipping credentials save (DuckDB connection required)")
            summary["success"] = False
            return summary

        # Save credentials to .dango/metabase.yml (gitignored)
        credentials = {
            "metabase_url": metabase_url,
            "admin": {
                "email": admin_email,
                "password": admin_password
            },
            "database": {
                "id": summary.get("duckdb_id"),
                "name": f"{org_name} Analytics"
            },
            "setup_completed_at": datetime.now().isoformat()
        }

        credentials_file.parent.mkdir(parents=True, exist_ok=True)
        with open(credentials_file, 'w') as f:
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


def sync_metabase_schema(
    project_root: Path,
    metabase_url: str = "http://localhost:3000"
) -> bool:
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

    try:
        # Load credentials
        with open(credentials_file, 'r') as f:
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
        login_response = requests.post(
            f"{metabase_url}/api/session",
            json={"username": email, "password": password},
            timeout=10
        )

        if login_response.status_code != 200:
            return False

        session_id = login_response.json().get("id")
        if not session_id:
            return False

        # Trigger sync
        response = requests.post(
            f"{metabase_url}/api/database/{database_id}/sync_schema",
            headers={"X-Metabase-Session": session_id},
            timeout=10
        )

        if response.status_code != 200:
            return False

        # Wait for sync to complete (poll up to 10 seconds)
        import time
        for _ in range(10):
            time.sleep(1)
            db_status = requests.get(
                f"{metabase_url}/api/database/{database_id}",
                headers={"X-Metabase-Session": session_id},
                timeout=5
            )
            if db_status.status_code == 200:
                # Check if sync is complete (no longer has 'initial_sync_status')
                db_data = db_status.json()
                if not db_data.get("initial_sync_status") or db_data.get("initial_sync_status") == "complete":
                    break

        # Update table descriptions to guide users
        try:
            # Get all tables
            metadata_response = requests.get(
                f"{metabase_url}/api/database/{database_id}/metadata",
                headers={"X-Metabase-Session": session_id},
                timeout=10
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
                    else:
                        continue  # Skip tables without clear guidance

                    # Update table description and visibility
                    update_payload = {"description": description}
                    if visibility_type:
                        update_payload["visibility_type"] = visibility_type

                    response = requests.put(
                        f"{metabase_url}/api/table/{table_id}",
                        headers={"X-Metabase-Session": session_id},
                        json=update_payload,
                        timeout=5
                    )
                    if response.status_code != 200:
                        logger.warning(
                            f"Failed to update table {schema}.{table_name} (id={table_id}): "
                            f"status={response.status_code}, response={response.text[:200]}"
                        )

        except Exception as e:
            # Log but don't fail - descriptions are nice-to-have
            logger.warning(f"Error updating table metadata: {e}")

        return True

    except Exception:
        # Silent failure - don't block sync if Metabase isn't running
        return False


def refresh_metabase_connection(
    project_root: Path,
    metabase_url: str = "http://localhost:3000"
) -> bool:
    """
    Force Metabase to refresh its DuckDB connection to see latest data.

    This is needed because DuckDB connections hold a snapshot of the database.
    After loading new data, Metabase needs to restart to see the changes.

    Args:
        project_root: Path to project root
        metabase_url: Metabase URL

    Returns:
        True if refresh succeeded, False otherwise
    """
    import subprocess

    try:
        # Get container name from project root
        project_name = project_root.name
        container_name = f"{project_name}-metabase-1"

        # Check if container exists and is running
        check_result = subprocess.run(
            ["docker", "ps", "--filter", f"name={container_name}", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=5
        )

        if container_name not in check_result.stdout:
            # Container not running
            return False

        # Restart Metabase container to force reconnection
        restart_result = subprocess.run(
            ["docker", "restart", container_name],
            capture_output=True,
            text=True,
            timeout=30
        )

        if restart_result.returncode != 0:
            return False

        # Wait for Metabase to come back up (max 20 seconds)
        max_attempts = 20
        for _ in range(max_attempts):
            try:
                response = requests.get(f"{metabase_url}/api/health", timeout=1)
                if response.status_code == 200:
                    return True
            except requests.exceptions.RequestException:
                pass
            time.sleep(1)

        return False

    except Exception:
        # Silent failure
        return False
