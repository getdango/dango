"""dango/cli/schema_manager.py

Automatically generates and updates schema.yml files for intermediate/marts models after successful dbt runs. Preserves user-written descriptions while syncing column lists.
"""

from pathlib import Path
from typing import Any

import duckdb
import yaml
from rich.console import Console

console = Console()


class SchemaManager:
    """Manages schema.yml generation and updates for dbt models"""

    def __init__(self, project_root: Path, duckdb_path: Path):
        """
        Initialize schema manager

        Args:
            project_root: Path to project root
            duckdb_path: Path to DuckDB database
        """
        self.project_root = project_root
        self.duckdb_path = duckdb_path
        self.dbt_dir = project_root / "dbt"
        self.models_dir = self.dbt_dir / "models"

    def update_schemas_for_models(self, model_names: list[str]) -> None:
        """
        Update schema.yml files for list of models

        Args:
            model_names: List of model names that were run
        """
        # Filter to only intermediate/marts models
        for model_name in model_names:
            # Determine layer from manifest or file location
            layer = self._get_model_layer(model_name)

            if layer in ["intermediate", "marts"]:
                self._update_schema_for_model(model_name, layer)

    def _get_model_layer(self, model_name: str) -> str | None:
        """
        Determine which layer a model belongs to

        Args:
            model_name: Name of the model

        Returns:
            Layer name (intermediate, marts, staging) or None
        """
        from dango.transformation.model_service import ModelServiceError, find_model

        try:
            found = find_model(self.project_root, model_name)
        except ModelServiceError:
            return None
        return found[1] if found else None

    def _update_schema_for_model(self, model_name: str, layer: str) -> None:
        """
        Update schema.yml for a specific model

        Args:
            model_name: Name of the model
            layer: Layer (intermediate or marts)
        """
        # Get actual columns from DuckDB
        columns = self._introspect_model_columns(model_name, layer)

        if not columns:
            # Model doesn't exist in DB yet (run failed?)
            return

        # Schema.yml path: the file already documenting this model (may be hand-made or
        # nested), else the layer default
        from dango.transformation.model_docs import resolve_docs_path
        from dango.transformation.model_service import ModelServiceError

        try:
            schema_path = resolve_docs_path(self.project_root, layer, model_name)
        except ModelServiceError as e:
            console.print(f"[yellow]⚠️[/yellow]  Skipping schema.yml for {model_name}: {e}")
            return

        # Load existing schema.yml or create new
        existing_schema = self._load_schema_yml(schema_path)

        # Merge schemas
        updated_schema, changes = self._merge_schema(model_name, columns, existing_schema, layer)

        # Write updated schema
        self._write_schema_yml(schema_path, updated_schema)

        # Notify user of changes
        self._report_changes(model_name, changes, schema_path)

    def _introspect_model_columns(self, model_name: str, layer: str) -> list[dict[str, str]]:
        """
        Introspect DuckDB to get actual columns for a model

        Args:
            model_name: Name of the model
            layer: Schema/layer name

        Returns:
            List of dicts with 'name' and 'type' keys
        """
        try:
            conn = duckdb.connect(str(self.duckdb_path), config={"access_mode": "read_only"})
            try:
                # Query information schema for columns
                query = f"""
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = '{layer}'
                      AND table_name = '{model_name}'
                    ORDER BY ordinal_position
                """

                result = conn.execute(query).fetchall()
                return [{"name": col_name, "type": col_type} for col_name, col_type in result]
            finally:
                conn.close()

        except Exception:
            # Model might not exist yet
            return []

    def _load_schema_yml(self, schema_path: Path) -> dict[str, Any] | None:
        """
        Load existing schema.yml file

        Args:
            schema_path: Path to schema.yml

        Returns:
            Parsed YAML dict, or None if file doesn't exist
        """
        if not schema_path.exists():
            return None

        try:
            with open(schema_path) as f:
                result = yaml.safe_load(f)
                return result if isinstance(result, dict) else None
        except Exception:
            # Corrupted YAML - return None to regenerate
            return None

    def _merge_schema(
        self,
        model_name: str,
        actual_columns: list[dict[str, str]],
        existing_schema: dict[str, Any] | None,
        layer: str = "marts",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Merge actual columns with existing schema, preserving descriptions

        Args:
            model_name: Name of the model
            actual_columns: Actual columns from DuckDB
            existing_schema: Existing schema.yml content

        Returns:
            Tuple of (updated_schema, changes_dict)
        """
        added_columns: list[str] = []
        removed_columns: list[dict[str, str]] = []
        changes: dict[str, Any] = {
            "is_new": False,
            "added_columns": added_columns,
            "removed_columns": removed_columns,
            "preserved_columns": 0,
        }

        # Find existing model in schema
        existing_model = None
        if existing_schema and "models" in existing_schema:
            for model in existing_schema["models"]:
                if model.get("name") == model_name:
                    existing_model = model
                    break

        # Build column name -> description map from existing
        existing_descriptions = {}
        existing_columns: dict[str, dict[str, Any]] = {}
        if existing_model and "columns" in existing_model:
            for col in existing_model["columns"]:
                col_name = col.get("name")
                col_desc = col.get("description", "")
                if col_name:
                    existing_descriptions[col_name] = col_desc
                    existing_columns[col_name] = col

        # Build new columns list
        new_columns = []
        for col in actual_columns:
            col_name = col["name"]

            if col_name in existing_descriptions:
                # Preserve existing description
                description = existing_descriptions[col_name]
                changes["preserved_columns"] += 1
            else:
                # New column - add helpful placeholder
                description = "TODO: Add description\n(Auto-generated - edit in dbt/models/[layer]/schema.yml)"
                added_columns.append(col_name)

            # Keep every other key the user wrote (data_tests, meta, ...)
            entry = dict(existing_columns.get(col_name, {}))
            entry["name"] = col_name
            entry["description"] = description
            new_columns.append(entry)

        # Detect removed columns
        actual_col_names = {col["name"] for col in actual_columns}
        for col_name, description in existing_descriptions.items():
            if col_name not in actual_col_names:
                removed_columns.append({"name": col_name, "description": description})

        # Build model entry — generate a default description for new models
        if existing_model:
            description = existing_model.get("description", "")
        else:
            # Auto-generate from model name: int_foo_bar → "Foo bar"
            readable = model_name
            for prefix in ("int_", "intermediate_"):
                if readable.startswith(prefix):
                    readable = readable[len(prefix) :]
                    break
            readable = readable.replace("_", " ").strip().capitalize()
            description = f"{layer.capitalize()} model: {readable}"

        # Start from the existing entry so config, data_tests, meta, ... survive
        new_model: dict[str, Any] = dict(existing_model) if existing_model else {}
        new_model["name"] = model_name
        new_model["description"] = description
        new_model["columns"] = new_columns

        # Build full schema structure, keeping other top-level keys (sources, exposures, ...)
        updated_schema = dict(existing_schema) if existing_schema else {}
        existing_models = updated_schema.get("models")
        updated_models = []
        found = False
        for model in existing_models if isinstance(existing_models, list) else []:
            if isinstance(model, dict) and model.get("name") == model_name:
                updated_models.append(new_model)
                found = True
            else:
                updated_models.append(model)
        if not found:
            updated_models.append(new_model)
            changes["is_new"] = True
        updated_schema["version"] = updated_schema.get("version", 2)
        updated_schema["models"] = updated_models

        return updated_schema, changes

    def _write_schema_yml(self, schema_path: Path, schema: dict[str, Any]) -> None:
        """
        Write schema.yml file

        Args:
            schema_path: Path to schema.yml
            schema: Schema dict to write
        """
        schema_path.parent.mkdir(parents=True, exist_ok=True)

        with open(schema_path, "w") as f:
            yaml.dump(schema, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def _report_changes(self, model_name: str, changes: dict[str, Any], schema_path: Path) -> None:
        """
        Report changes to user

        Args:
            model_name: Name of the model
            changes: Changes dict from merge
            schema_path: Path to schema.yml
        """
        if changes["is_new"]:
            console.print(f"[green]✓[/green] Created schema.yml for [cyan]{model_name}[/cyan]")
            console.print(f"  [dim]File: {schema_path.relative_to(self.project_root)}[/dim]")
            console.print(
                f"  [dim]Columns: {len(changes['added_columns'])} (add descriptions to document)[/dim]"
            )
            return

        # Check if there were any changes
        has_changes = len(changes["added_columns"]) > 0 or len(changes["removed_columns"]) > 0

        if not has_changes:
            # No changes, don't report anything
            return

        console.print(f"[yellow]⚠️[/yellow]  Schema changes for [cyan]{model_name}[/cyan]:")

        if changes["added_columns"]:
            for col_name in changes["added_columns"]:
                console.print(
                    f"  [green]+[/green] Added: {col_name} [dim](description needed)[/dim]"
                )

        if changes["removed_columns"]:
            for col in changes["removed_columns"]:
                col_name = col["name"]
                col_desc = col["description"]
                if col_desc:
                    console.print(
                        f'  [red]-[/red] Removed: {col_name} [dim](had description: "{col_desc}")[/dim]'
                    )
                else:
                    console.print(f"  [red]-[/red] Removed: {col_name}")

        if changes["preserved_columns"] > 0:
            console.print(
                f"  [green]✓[/green] Preserved descriptions for {changes['preserved_columns']} existing columns"
            )


def update_model_schemas(project_root: Path, model_names: list[str]) -> None:
    """
    Update schema.yml files for models after successful dbt run

    Args:
        project_root: Path to project root
        model_names: List of model names that were run
    """
    from dango.config import ConfigLoader

    loader = ConfigLoader(project_root)
    config = loader.load_config()

    duckdb_path = project_root / config.platform.duckdb_path

    manager = SchemaManager(project_root, duckdb_path)
    manager.update_schemas_for_models(model_names)
