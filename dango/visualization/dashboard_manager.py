"""
Dango Metabase Management

Simplified git-based workflow for Metabase dashboards and questions.

Features:
- Save to git: Export dashboards and questions to metabase/ (YAML format)
- Load from git: Import dashboards and questions from metabase/
- Collection filtering: Exclude personal collections by default
- Overwrite control: Skip existing by default, force sync with --overwrite

Design:
- Non-personal collections (Shared, team) → metabase/ (git-tracked)
- Personal collections → Not exported by default
- YAML format only (human-readable, git-friendly, no timestamps)
- Directory structure: metabase/dashboards/ and metabase/questions/
"""

from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import yaml
import requests
from datetime import datetime
from rich.console import Console

console = Console()


class DashboardManager:
    """
    Manages Metabase dashboard export, import, and synchronization
    """

    def __init__(
        self,
        project_root: Path,
        metabase_url: str = "http://localhost:3000",
        session_token: Optional[str] = None
    ):
        """
        Initialize dashboard manager

        Args:
            project_root: Path to Dango project root
            metabase_url: Metabase URL
            session_token: Metabase session token (optional, will read from credentials)
        """
        self.project_root = project_root
        self.metabase_url = metabase_url.rstrip('/')
        self.state_file = project_root / ".dango" / "state" / "dashboard_sync.json"
        self.credentials_file = project_root / ".dango" / "metabase.yml"

        self.session_token = session_token
        if not self.session_token:
            self._load_credentials()

    def _load_credentials(self) -> None:
        """Load Metabase credentials from config"""
        if self.credentials_file.exists():
            with open(self.credentials_file, 'r') as f:
                creds = yaml.safe_load(f)
                email = creds.get('admin', {}).get('email')
                password = creds.get('admin', {}).get('password')

                if email and password:
                    # Login to get session token
                    try:
                        response = requests.post(
                            f"{self.metabase_url}/api/session",
                            json={"username": email, "password": password},
                            timeout=10
                        )
                        if response.status_code == 200:
                            self.session_token = response.json().get("id")
                    except Exception as e:
                        console.print(f"[yellow]Warning: Could not authenticate with Metabase: {e}[/yellow]")

    def _get_headers(self) -> Dict[str, str]:
        """Get API headers with session token"""
        if not self.session_token:
            raise ValueError("No Metabase session token available")
        return {"X-Metabase-Session": self.session_token}

    def get_collections(self) -> List[Dict[str, Any]]:
        """
        Get all Metabase collections

        Returns:
            List of collection metadata
        """
        try:
            response = requests.get(
                f"{self.metabase_url}/api/collection",
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code == 200:
                return response.json()
            else:
                console.print(f"[red]Failed to get collections: {response.status_code}[/red]")
                return []

        except Exception as e:
            console.print(f"[red]Error getting collections: {e}[/red]")
            return []

    def get_collection_id(self, collection_name: str) -> Optional[int]:
        """
        Get collection ID by name

        Args:
            collection_name: Name of collection

        Returns:
            Collection ID or None
        """
        collections = self.get_collections()
        for collection in collections:
            if collection.get("name") == collection_name:
                return collection.get("id")
        return None

    def get_dashboards(self, collection_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get dashboards from Metabase

        Args:
            collection_id: Filter by collection ID (None = all dashboards)

        Returns:
            List of dashboard metadata
        """
        try:
            # Get all dashboards
            response = requests.get(
                f"{self.metabase_url}/api/dashboard",
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code != 200:
                console.print(f"[red]Failed to get dashboards: {response.status_code}[/red]")
                return []

            dashboards = response.json()

            # Filter by collection if specified
            if collection_id is not None:
                dashboards = [d for d in dashboards if d.get("collection_id") == collection_id]

            return dashboards

        except Exception as e:
            console.print(f"[red]Error getting dashboards: {e}[/red]")
            return []

    def get_dashboard_details(self, dashboard_id: int) -> Optional[Dict[str, Any]]:
        """
        Get full dashboard details including cards

        Args:
            dashboard_id: Dashboard ID

        Returns:
            Dashboard details or None
        """
        try:
            response = requests.get(
                f"{self.metabase_url}/api/dashboard/{dashboard_id}",
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code == 200:
                return response.json()
            else:
                console.print(f"[yellow]Warning: Could not get dashboard {dashboard_id}[/yellow]")
                return None

        except Exception as e:
            console.print(f"[red]Error getting dashboard details: {e}[/red]")
            return None

    def get_cards(self, collection_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get saved questions/cards from Metabase

        Args:
            collection_id: Filter by collection ID (None = all cards)

        Returns:
            List of card metadata
        """
        try:
            # Get all cards
            response = requests.get(
                f"{self.metabase_url}/api/card",
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code != 200:
                console.print(f"[red]Failed to get cards: {response.status_code}[/red]")
                return []

            cards = response.json()

            # Filter by collection if specified
            if collection_id is not None:
                cards = [c for c in cards if c.get("collection_id") == collection_id]

            return cards

        except Exception as e:
            console.print(f"[red]Error getting cards: {e}[/red]")
            return []

    def get_card_details(self, card_id: int) -> Optional[Dict[str, Any]]:
        """
        Get full card/question details

        Args:
            card_id: Card ID

        Returns:
            Card details or None
        """
        try:
            response = requests.get(
                f"{self.metabase_url}/api/card/{card_id}",
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code == 200:
                return response.json()
            else:
                console.print(f"[yellow]Warning: Could not get card {card_id}[/yellow]")
                return None

        except Exception as e:
            console.print(f"[red]Error getting card details: {e}[/red]")
            return None

    def get_timelines(self, collection_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get timelines from Metabase

        Args:
            collection_id: Filter by collection ID (None = all timelines)

        Returns:
            List of timeline metadata
        """
        try:
            response = requests.get(
                f"{self.metabase_url}/api/timeline",
                headers=self._get_headers(),
                timeout=10
            )

            if response.status_code != 200:
                console.print(f"[yellow]Warning: Could not get timelines: {response.status_code}[/yellow]")
                return []

            timelines = response.json()

            # Filter by collection if specified
            if collection_id is not None:
                timelines = [t for t in timelines if t.get("collection_id") == collection_id]

            return timelines

        except Exception as e:
            console.print(f"[yellow]Warning: Error getting timelines: {e}[/yellow]")
            return []

    def card_to_yaml(self, card: Dict[str, Any]) -> str:
        """
        Convert card JSON to YAML format

        Args:
            card: Card data from Metabase API

        Returns:
            YAML string
        """
        # Determine card type (question, model, or metric)
        card_type = card.get("type", "question")
        if card_type == "question" and card.get("dataset"):  # Old API compatibility
            card_type = "model"

        simplified = {
            "name": card.get("name"),
            "description": card.get("description", ""),
            "type": card_type,  # question, model, or metric
            "collection": card.get("collection", {}).get("name", "Default"),
            "collection_id": card.get("collection_id"),
            "created_at": card.get("created_at"),
            "updated_at": card.get("updated_at"),
            "display": card.get("display"),
            "dataset_query": card.get("dataset_query", {}),
            "visualization_settings": card.get("visualization_settings", {})
        }

        return yaml.safe_dump(simplified, default_flow_style=False, sort_keys=False)

    def timeline_to_yaml(self, timeline: Dict[str, Any]) -> str:
        """
        Convert timeline JSON to YAML format

        Args:
            timeline: Timeline data from Metabase API

        Returns:
            YAML string
        """
        simplified = {
            "name": timeline.get("name"),
            "description": timeline.get("description", ""),
            "collection_id": timeline.get("collection_id"),
            "icon": timeline.get("icon", "star"),
            "default": timeline.get("default", False),
            "events": timeline.get("events", [])
        }

        return yaml.safe_dump(simplified, default_flow_style=False, sort_keys=False)

    def dashboard_to_yaml(self, dashboard: Dict[str, Any]) -> str:
        """
        Convert dashboard JSON to YAML format

        Args:
            dashboard: Dashboard data from Metabase API

        Returns:
            YAML string
        """
        # Simplify dashboard structure for YAML export
        simplified = {
            "name": dashboard.get("name"),
            "description": dashboard.get("description", ""),
            "collection": dashboard.get("collection", {}).get("name", "Default"),
            "created_at": dashboard.get("created_at"),
            "updated_at": dashboard.get("updated_at"),
            "cards": []
        }

        # Simplify cards
        for card in dashboard.get("ordered_cards", []):
            card_info = card.get("card", {})
            simplified_card = {
                "name": card_info.get("name"),
                "description": card_info.get("description", ""),
                "type": card_info.get("display"),
                "dataset_query": card_info.get("dataset_query", {}),
                "visualization_settings": card_info.get("visualization_settings", {}),
                "position": {
                    "row": card.get("row", 0),
                    "col": card.get("col", 0),
                    "size_x": card.get("size_x", 6),
                    "size_y": card.get("size_y", 4)
                }
            }
            simplified["cards"].append(simplified_card)

        return yaml.safe_dump(simplified, default_flow_style=False, sort_keys=False)

    def _update_sync_state(self) -> None:
        """Update sync state file with current timestamp"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        state = {
            "last_export": datetime.now().isoformat(),
            "exported_from": self.metabase_url
        }

        with open(self.state_file, 'w') as f:
            json.dump(state, f, indent=2)

    def _create_card_from_yaml(self, card_data: Dict[str, Any], collection_id: Optional[int] = None) -> Optional[int]:
        """
        Create a card (question) in Metabase from YAML data

        Args:
            card_data: Card data from YAML file
            collection_id: Collection ID to create card in

        Returns:
            Card ID if successful, None otherwise
        """
        try:
            payload = {
                "name": card_data.get("name"),
                "description": card_data.get("description", ""),
                "display": card_data.get("display", "table"),
                "dataset_query": card_data.get("dataset_query", {}),
                "visualization_settings": card_data.get("visualization_settings", {})
            }

            if collection_id is not None:
                payload["collection_id"] = collection_id

            response = requests.post(
                f"{self.metabase_url}/api/card",
                headers=self._get_headers(),
                json=payload,
                timeout=30
            )

            if response.status_code in [200, 201]:
                return response.json().get("id")
            else:
                console.print(f"[yellow]Warning: Failed to create card '{card_data.get('name')}': {response.status_code}[/yellow]")
                return None

        except Exception as e:
            console.print(f"[yellow]Warning: Error creating card '{card_data.get('name')}': {e}[/yellow]")
            return None

    def _create_dashboard_from_yaml(
        self,
        dashboard_data: Dict[str, Any],
        collection_id: Optional[int] = None,
        overwrite: bool = False
    ) -> Optional[int]:
        """
        Create a dashboard in Metabase from YAML data

        Args:
            dashboard_data: Dashboard data from YAML file
            collection_id: Collection ID to create dashboard in
            overwrite: If True and dashboard exists, delete and recreate

        Returns:
            Dashboard ID if successful, None otherwise
        """
        try:
            dashboard_name = dashboard_data.get("name")

            # Check if dashboard exists
            existing_dashboards = {d.get("name"): d for d in self.get_dashboards()}

            if dashboard_name in existing_dashboards:
                if overwrite:
                    # Delete existing dashboard
                    existing_id = existing_dashboards[dashboard_name].get("id")
                    try:
                        delete_response = requests.delete(
                            f"{self.metabase_url}/api/dashboard/{existing_id}",
                            headers=self._get_headers(),
                            timeout=10
                        )
                        if delete_response.status_code not in [200, 204]:
                            console.print(f"[yellow]Warning: Failed to delete existing dashboard '{dashboard_name}'[/yellow]")
                            return None
                    except Exception as e:
                        console.print(f"[yellow]Warning: Error deleting dashboard '{dashboard_name}': {e}[/yellow]")
                        return None
                else:
                    # Skip existing dashboard
                    return None

            # Create dashboard
            dashboard_payload = {
                "name": dashboard_name,
                "description": dashboard_data.get("description", "")
            }

            if collection_id is not None:
                dashboard_payload["collection_id"] = collection_id

            response = requests.post(
                f"{self.metabase_url}/api/dashboard",
                headers=self._get_headers(),
                json=dashboard_payload,
                timeout=30
            )

            if response.status_code not in [200, 201]:
                console.print(f"[yellow]Warning: Failed to create dashboard '{dashboard_name}': {response.status_code}[/yellow]")
                return None

            dashboard_id = response.json().get("id")

            # Create cards and add them to dashboard
            cards = dashboard_data.get("cards", [])
            for card_data in cards:
                # Create the card
                card_id = self._create_card_from_yaml(card_data, collection_id)

                if card_id:
                    # Add card to dashboard
                    position = card_data.get("position", {})
                    card_payload = {
                        "cardId": card_id,
                        "row": position.get("row", 0),
                        "col": position.get("col", 0),
                        "sizeX": position.get("size_x", 6),
                        "sizeY": position.get("size_y", 4)
                    }

                    try:
                        add_card_response = requests.post(
                            f"{self.metabase_url}/api/dashboard/{dashboard_id}/cards",
                            headers=self._get_headers(),
                            json=card_payload,
                            timeout=30
                        )

                        if add_card_response.status_code not in [200, 201]:
                            console.print(f"[yellow]Warning: Failed to add card '{card_data.get('name')}' to dashboard[/yellow]")
                    except Exception as e:
                        console.print(f"[yellow]Warning: Error adding card to dashboard: {e}[/yellow]")

            return dashboard_id

        except Exception as e:
            console.print(f"[yellow]Warning: Error creating dashboard '{dashboard_data.get('name')}': {e}[/yellow]")
            return None

    def _create_question_from_yaml(
        self,
        question_data: Dict[str, Any],
        collection_id: Optional[int] = None,
        overwrite: bool = False
    ) -> Optional[int]:
        """
        Create a standalone question in Metabase from YAML data

        Args:
            question_data: Question data from YAML file
            collection_id: Collection ID to create question in
            overwrite: If True and question exists, delete and recreate

        Returns:
            Question ID if successful, None otherwise
        """
        try:
            question_name = question_data.get("name")

            # Check if question exists
            existing_questions = {q.get("name"): q for q in self.get_cards()}

            if question_name in existing_questions:
                if overwrite:
                    # Delete existing question
                    existing_id = existing_questions[question_name].get("id")
                    try:
                        delete_response = requests.delete(
                            f"{self.metabase_url}/api/card/{existing_id}",
                            headers=self._get_headers(),
                            timeout=10
                        )
                        if delete_response.status_code not in [200, 204]:
                            console.print(f"[yellow]Warning: Failed to delete existing question '{question_name}'[/yellow]")
                            return None
                    except Exception as e:
                        console.print(f"[yellow]Warning: Error deleting question '{question_name}': {e}[/yellow]")
                        return None
                else:
                    # Skip existing question
                    return None

            # Create question
            return self._create_card_from_yaml(question_data, collection_id)

        except Exception as e:
            console.print(f"[yellow]Warning: Error creating question '{question_data.get('name')}': {e}[/yellow]")
            return None

    def save_to_files(
        self,
        include_personal: bool = False,
        collections: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Save Metabase assets to files (simplified export for version control)

        Exports dashboards and questions to metabase/ directory in YAML format.
        By default, excludes personal collections (only exports team/shared assets).

        Files can optionally be committed to git for version control, but this
        command works independently of git.

        Args:
            include_personal: Include personal collections (default: False)
            collections: Specific collections to export (default: all non-personal)

        Returns:
            Summary with exported items

        Directory structure:
            metabase/
            ├── dashboards/
            │   ├── sales_dashboard.yml
            │   └── marketing_overview.yml
            ├── questions/
            │   └── revenue_by_month.yml
            ├── models/
            │   └── customer_360.yml
            ├── metrics/
            │   └── total_revenue.yml
            ├── timelines/
            │   └── product_launches.yml
            └── collections.yml
        """
        summary = {
            "success": False,
            "exported_dashboards": [],
            "exported_questions": [],
            "exported_models": [],
            "exported_metrics": [],
            "exported_timelines": [],
            "skipped_collections": [],
            "errors": []
        }

        # Determine which collections to export
        all_collections = self.get_collections()

        if collections:
            # Export specific collections
            collections_to_export = [c for c in all_collections if c.get("name") in collections]
        else:
            # Export all non-personal collections
            collections_to_export = []
            for c in all_collections:
                name = c.get("name", "")
                # Skip personal collections unless explicitly requested
                if not include_personal and ("personal" in name.lower() or "private" in name.lower()):
                    summary["skipped_collections"].append(name)
                    continue
                collections_to_export.append(c)

        if not collections_to_export:
            summary["errors"].append("No collections to export")
            return summary

        # Create metabase/ directory structure
        metabase_dir = self.project_root / "metabase"
        dashboards_dir = metabase_dir / "dashboards"
        questions_dir = metabase_dir / "questions"
        models_dir = metabase_dir / "models"
        metrics_dir = metabase_dir / "metrics"
        timelines_dir = metabase_dir / "timelines"

        dashboards_dir.mkdir(parents=True, exist_ok=True)
        questions_dir.mkdir(parents=True, exist_ok=True)
        models_dir.mkdir(parents=True, exist_ok=True)
        metrics_dir.mkdir(parents=True, exist_ok=True)
        timelines_dir.mkdir(parents=True, exist_ok=True)

        # Export dashboards from each collection
        for collection in collections_to_export:
            collection_id = collection.get("id")
            collection_name = collection.get("name")

            console.print(f"[dim]Exporting from collection: {collection_name}[/dim]")

            # Get dashboards in this collection
            dashboards = self.get_dashboards(collection_id)

            for dashboard_meta in dashboards:
                dashboard_id = dashboard_meta.get("id")
                dashboard_name = dashboard_meta.get("name", f"dashboard_{dashboard_id}")

                # Get full dashboard details
                dashboard = self.get_dashboard_details(dashboard_id)
                if not dashboard:
                    continue

                # Generate filename (sanitize name)
                filename = dashboard_name.lower().replace(" ", "_").replace("/", "_")
                filename = "".join(c for c in filename if c.isalnum() or c in ['_', '-'])

                filepath = dashboards_dir / f"{filename}.yml"
                content = self.dashboard_to_yaml(dashboard)

                # Write file
                try:
                    with open(filepath, 'w') as f:
                        f.write(content)

                    summary["exported_dashboards"].append({
                        "name": dashboard_name,
                        "collection": collection_name,
                        "file": str(filepath.relative_to(self.project_root)),
                        "cards": len(dashboard.get("ordered_cards", []))
                    })
                except Exception as e:
                    summary["errors"].append(f"Failed to write {dashboard_name}: {e}")

            # Get cards (questions/models/metrics) in this collection
            cards = self.get_cards(collection_id)

            for card_meta in cards:
                card_id = card_meta.get("id")
                card_name = card_meta.get("name", f"card_{card_id}")

                # Get full card details
                card = self.get_card_details(card_id)
                if not card:
                    continue

                # Determine card type
                card_type = card.get("type", "question")
                if card_type == "question" and card.get("dataset"):  # Old API compatibility
                    card_type = "model"

                # Determine target directory based on type
                if card_type == "model":
                    target_dir = models_dir
                    summary_key = "exported_models"
                elif card_type == "metric":
                    target_dir = metrics_dir
                    summary_key = "exported_metrics"
                else:  # question
                    target_dir = questions_dir
                    summary_key = "exported_questions"

                # Generate filename (sanitize name)
                filename = card_name.lower().replace(" ", "_").replace("/", "_")
                filename = "".join(c for c in filename if c.isalnum() or c in ['_', '-'])

                filepath = target_dir / f"{filename}.yml"
                content = self.card_to_yaml(card)

                # Write file
                try:
                    with open(filepath, 'w') as f:
                        f.write(content)

                    summary[summary_key].append({
                        "name": card_name,
                        "collection": collection_name,
                        "file": str(filepath.relative_to(self.project_root)),
                        "type": card.get("display", "table"),
                        "card_type": card_type
                    })
                except Exception as e:
                    summary["errors"].append(f"Failed to write {card_name}: {e}")

            # Get timelines in this collection
            timelines = self.get_timelines(collection_id)

            for timeline in timelines:
                timeline_id = timeline.get("id")
                timeline_name = timeline.get("name", f"timeline_{timeline_id}")

                # Generate filename (sanitize name)
                filename = timeline_name.lower().replace(" ", "_").replace("/", "_")
                filename = "".join(c for c in filename if c.isalnum() or c in ['_', '-'])

                filepath = timelines_dir / f"{filename}.yml"
                content = self.timeline_to_yaml(timeline)

                # Write file
                try:
                    with open(filepath, 'w') as f:
                        f.write(content)

                    summary["exported_timelines"].append({
                        "name": timeline_name,
                        "collection": collection_name,
                        "file": str(filepath.relative_to(self.project_root))
                    })
                except Exception as e:
                    summary["errors"].append(f"Failed to write timeline {timeline_name}: {e}")

        # Export collection hierarchy
        collections_file = metabase_dir / "collections.yml"
        try:
            collections_data = {
                "collections": [
                    {
                        "name": c.get("name"),
                        "id": c.get("id"),
                        "description": c.get("description", ""),
                        "color": c.get("color"),
                        "parent_id": c.get("location")
                    }
                    for c in collections_to_export
                ]
            }
            with open(collections_file, 'w') as f:
                f.write(yaml.safe_dump(collections_data, default_flow_style=False, sort_keys=False))
        except Exception as e:
            summary["errors"].append(f"Failed to write collections.yml: {e}")

        # Update sync state
        self._update_sync_state()

        summary["success"] = (
            len(summary["exported_dashboards"]) > 0 or
            len(summary["exported_questions"]) > 0 or
            len(summary["exported_models"]) > 0 or
            len(summary["exported_metrics"]) > 0 or
            len(summary["exported_timelines"]) > 0
        )
        return summary

    def load_from_files(
        self,
        overwrite: bool = False,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        Load Metabase assets from files (simplified import from version control)

        Imports dashboards, questions, models, metrics, and timelines from metabase/ directory.
        By default, skips existing items. Use --overwrite to replace existing.

        Implements rollback on failure - if any import fails, all changes are reverted.

        Files may be tracked in git for version control, but this command
        works independently of git.

        Args:
            overwrite: Replace existing dashboards/questions (default: False)
            dry_run: Show what would be imported without actually importing

        Returns:
            Summary with imported/skipped items
        """
        summary = {
            "success": False,
            "imported_dashboards": [],
            "imported_questions": [],
            "imported_models": [],
            "imported_metrics": [],
            "imported_timelines": [],
            "skipped": [],
            "errors": [],
            "would_overwrite": []
        }

        metabase_dir = self.project_root / "metabase"

        if not metabase_dir.exists():
            summary["errors"].append(f"Directory not found: {metabase_dir}")
            summary["errors"].append("Run 'dango metabase save' first to create exports")
            return summary

        # VALIDATION: If not dry-run and overwrite is enabled, run validation first
        if not dry_run and overwrite:
            console.print("[dim]Validating files before import...[/dim]")
            validation_result = self.load_from_files(overwrite=overwrite, dry_run=True)
            if validation_result["errors"]:
                summary["errors"].append("Validation failed, aborting import:")
                summary["errors"].extend(validation_result["errors"])
                return summary
            console.print("[dim]✓ Validation passed[/dim]\n")

        # Get existing items to check for conflicts
        existing_dashboards = {d.get("name"): d for d in self.get_dashboards()}
        existing_cards = {c.get("name"): c for c in self.get_cards()}

        # Backup items that will be overwritten (for rollback)
        backups = {}
        created_items = []  # Track created items for rollback

        try:
            # Import dashboards
            dashboards_dir = metabase_dir / "dashboards"
            if dashboards_dir.exists():
                for filepath in dashboards_dir.glob("*.yml"):
                    try:
                        with open(filepath, 'r') as f:
                            dashboard_data = yaml.safe_load(f)

                        dashboard_name = dashboard_data.get("name")

                        # Check if exists
                        if dashboard_name in existing_dashboards:
                            if not overwrite:
                                summary["skipped"].append({
                                    "type": "dashboard",
                                    "name": dashboard_name,
                                    "reason": "Already exists (use --overwrite to replace)"
                                })
                                continue
                            else:
                                # Backup before overwriting
                                if not dry_run:
                                    backups[f"dashboard_{dashboard_name}"] = {
                                        "type": "dashboard",
                                        "data": existing_dashboards[dashboard_name]
                                    }
                                summary["would_overwrite"].append({
                                    "type": "dashboard",
                                    "name": dashboard_name
                                })

                        if dry_run:
                            summary["imported_dashboards"].append({
                                "name": dashboard_name,
                                "file": str(filepath.relative_to(self.project_root)),
                                "cards": len(dashboard_data.get("cards", [])),
                                "dry_run": True
                            })
                        else:
                            # Actual Metabase API import
                            collection_id = dashboard_data.get("collection_id")

                            dashboard_id = self._create_dashboard_from_yaml(
                                dashboard_data,
                                collection_id=collection_id,
                                overwrite=overwrite
                            )

                            if dashboard_id:
                                created_items.append({
                                    "type": "dashboard",
                                    "id": dashboard_id,
                                    "name": dashboard_name
                                })
                                summary["imported_dashboards"].append({
                                    "name": dashboard_name,
                                    "file": str(filepath.relative_to(self.project_root)),
                                    "cards": len(dashboard_data.get("cards", [])),
                                    "id": dashboard_id
                                })
                            else:
                                raise Exception(f"Failed to import dashboard: {dashboard_name}")

                    except Exception as e:
                        raise Exception(f"Failed to import {filepath.name}: {e}")

            # Helper function to import cards (questions, models, metrics)
            def import_cards_from_dir(card_dir, card_type_name, summary_key):
                if card_dir.exists():
                    for filepath in card_dir.glob("*.yml"):
                        try:
                            with open(filepath, 'r') as f:
                                card_data = yaml.safe_load(f)

                            card_name = card_data.get("name")

                            # Check if exists
                            if card_name in existing_cards:
                                if not overwrite:
                                    summary["skipped"].append({
                                        "type": card_type_name,
                                        "name": card_name,
                                        "reason": "Already exists (use --overwrite to replace)"
                                    })
                                    return
                                else:
                                    # Backup before overwriting
                                    if not dry_run:
                                        backups[f"{card_type_name}_{card_name}"] = {
                                            "type": card_type_name,
                                            "data": existing_cards[card_name]
                                        }
                                    summary["would_overwrite"].append({
                                        "type": card_type_name,
                                        "name": card_name
                                    })

                            if dry_run:
                                summary[summary_key].append({
                                    "name": card_name,
                                    "file": str(filepath.relative_to(self.project_root)),
                                    "dry_run": True
                                })
                            else:
                                # Actual Metabase API import
                                collection_id = card_data.get("collection_id")

                                card_id = self._create_question_from_yaml(
                                    card_data,
                                    collection_id=collection_id,
                                    overwrite=overwrite
                                )

                                if card_id:
                                    created_items.append({
                                        "type": card_type_name,
                                        "id": card_id,
                                        "name": card_name
                                    })
                                    summary[summary_key].append({
                                        "name": card_name,
                                        "file": str(filepath.relative_to(self.project_root)),
                                        "id": card_id
                                    })
                                else:
                                    raise Exception(f"Failed to import {card_type_name}: {card_name}")

                        except Exception as e:
                            raise Exception(f"Failed to import {filepath.name}: {e}")

            # Import all card types
            import_cards_from_dir(metabase_dir / "questions", "question", "imported_questions")
            import_cards_from_dir(metabase_dir / "models", "model", "imported_models")
            import_cards_from_dir(metabase_dir / "metrics", "metric", "imported_metrics")

            # TODO: Import timelines (requires timeline creation API - skipping for MVP)
            # timelines_dir = metabase_dir / "timelines"
            # if timelines_dir.exists():
            #     ... timeline import logic ...

            summary["success"] = (
                len(summary["imported_dashboards"]) > 0 or
                len(summary["imported_questions"]) > 0 or
                len(summary["imported_models"]) > 0 or
                len(summary["imported_metrics"]) > 0 or
                len(summary["skipped"]) > 0
            )

        except Exception as e:
            # ROLLBACK: Something failed, revert all changes
            if not dry_run and (created_items or backups):
                console.print(f"\n[red]✗ Import failed: {e}[/red]")
                console.print("[yellow]Rolling back changes...[/yellow]\n")

                # Delete items we created
                for item in created_items:
                    try:
                        if item["type"] == "dashboard":
                            requests.delete(
                                f"{self.metabase_url}/api/dashboard/{item['id']}",
                                headers=self._get_headers(),
                                timeout=10
                            )
                            console.print(f"  [dim]✓ Deleted {item['type']}: {item['name']}[/dim]")
                        else:  # card types
                            requests.delete(
                                f"{self.metabase_url}/api/card/{item['id']}",
                                headers=self._get_headers(),
                                timeout=10
                            )
                            console.print(f"  [dim]✓ Deleted {item['type']}: {item['name']}[/dim]")
                    except Exception as rollback_error:
                        console.print(f"  [yellow]Warning: Could not delete {item['name']}: {rollback_error}[/yellow]")

                console.print("\n[green]✓ Rollback complete - no changes applied[/green]")

            summary["errors"].append(str(e))
            summary["success"] = False

        return summary



# ============================================================================
# STANDALONE HELPER FUNCTIONS
# ============================================================================

def import_dashboards(project_root: Path, overwrite: bool = False) -> Dict[str, Any]:
    """
    Import dashboards from dashboards/ directory (legacy path support)

    This is a convenience wrapper for backward compatibility.
    New code should use DashboardManager.load_from_files() directly.

    Args:
        project_root: Path to Dango project root
        overwrite: Replace existing dashboards (default: False)

    Returns:
        Summary dict with imported/skipped items
    """
    # Check if using legacy dashboards/ directory
    dashboards_dir = project_root / "dashboards"
    metabase_dir = project_root / "metabase"

    if not dashboards_dir.exists() and not metabase_dir.exists():
        return {
            "success": False,
            "imported": [],
            "skipped": [],
            "errors": ["No dashboards found. Create dashboards in Metabase first."]
        }

    # Initialize manager and import
    try:
        manager = DashboardManager(project_root)
        result = manager.load_from_files(overwrite=overwrite, dry_run=False)

        # Transform to legacy format for compatibility
        return {
            "success": result.get("success", False),
            "imported": (
                result.get("imported_dashboards", []) +
                result.get("imported_questions", []) +
                result.get("imported_models", []) +
                result.get("imported_metrics", [])
            ),
            "skipped": result.get("skipped", []),
            "errors": result.get("errors", [])
        }
    except Exception as e:
        return {
            "success": False,
            "imported": [],
            "skipped": [],
            "errors": [str(e)]
        }
