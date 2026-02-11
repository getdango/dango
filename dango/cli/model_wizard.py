"""dango/cli/model_wizard.py

Simple wizard for creating intermediate and marts models. Staging models are auto-generated, so this wizard only handles intermediate and marts layers.
"""

from datetime import datetime
from pathlib import Path

import inquirer
from rich.console import Console
from rich.panel import Panel

from dango.config import ConfigLoader

console = Console()


class ModelWizard:
    """Interactive wizard for creating dbt models"""

    def __init__(self, project_root: Path):
        """
        Initialize model wizard

        Args:
            project_root: Path to Dango project root
        """
        self.project_root = project_root
        self.dbt_dir = project_root / "dbt"
        self.models_dir = self.dbt_dir / "models"

        # Load project config
        loader = ConfigLoader(project_root)
        self.config = loader.load_config()

    def run(self) -> Path | None:
        """
        Run the model creation wizard

        Returns:
            Path to created model file, or None if cancelled
        """
        console.print("\n[bold cyan]📝 dbt Model Wizard[/bold cyan]\n")
        console.print("Create a new intermediate or marts model.\n")
        console.print("[dim]Staging models are auto-generated during sync.[/dim]\n")

        # Check if dbt directory exists
        if not self.dbt_dir.exists():
            console.print("[red]Error: dbt directory not found. Run 'dango init' first.[/red]")
            return None

        try:
            # Ask questions
            model_layer = self._ask_layer()
            if not model_layer:
                console.print("[yellow]Cancelled[/yellow]")
                return None

            model_name = self._ask_name(model_layer)
            if not model_name:
                console.print("[yellow]Cancelled[/yellow]")
                return None

            description = self._ask_description()
            materialization = self._ask_materialization(model_layer)

            # Generate model file
            model_path = self._create_model_file(
                layer=model_layer,
                name=model_name,
                description=description,
                materialization=materialization,
            )

            # Check if file creation failed (already exists)
            if model_path is None:
                return None

            # Regenerate manifest.json so model appears in Web UI
            console.print("\n[dim]Regenerating dbt manifest...[/dim]")
            self._regenerate_manifest()

            # Success message
            console.print()

            # Build next steps based on layer
            # Get port from config for URLs
            port = self.config.platform.port
            metabase_url = f"http://localhost:{port}/metabase"
            dbt_docs_url = f"http://localhost:{port}/dbt-docs"

            # Check if platform is running
            import socket

            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            platform_running = sock.connect_ex(("127.0.0.1", port)) == 0
            sock.close()

            if model_layer == "marts":
                metabase_instruction = (
                    "4. View in Metabase:\n   • After running, table appears in 'marts' schema\n"
                )
                if platform_running:
                    metabase_instruction += f"   • Access at [cyan]{metabase_url}[/cyan]\n\n"
                else:
                    metabase_instruction += "   • Start platform first: [cyan]dango start[/cyan]\n"
                    metabase_instruction += f"   • Then access at [cyan]{metabase_url}[/cyan]\n\n"

                next_steps = (
                    f"[dim]Next Steps:[/dim]\n\n"
                    f"1. Edit your transformation:\n"
                    f"   • File: {model_path.relative_to(self.project_root)}\n"
                    f"   • Write SQL to transform your data (see examples in file)\n"
                    f"   • Marts are final tables for business reporting/dashboards\n\n"
                    f"2. Run your model to create the table:\n"
                    f"   [cyan]dango run --select {model_name.replace('.sql', '')}[/cyan]\n"
                    f"   • Creates table in DuckDB\n"
                    f"   • [bold]AUTOMATED:[/bold] schema.yml auto-generated with all columns\n"
                    f"   • [bold]AUTOMATED:[/bold] Metabase connection refreshed\n"
                    f"   [dim]• Use +model to run dependencies first: [cyan]--select +{model_name.replace('.sql', '')}[/cyan]\n"
                    f"   • Use model+ to run this + downstream: [cyan]--select {model_name.replace('.sql', '')}+[/cyan]\n"
                    f"   • Use +model+ for both: [cyan]--select +{model_name.replace('.sql', '')}+[/cyan][/dim]\n\n"
                    f"3. Add descriptions to your table:\n"
                    f"   • Open auto-generated: dbt/models/marts/schema.yml\n"
                    f"   • Fill in description: fields for table and columns\n"
                    f"   • Regenerate docs: [cyan]dango docs[/cyan]\n"
                    f"   • View docs at [cyan]{dbt_docs_url}[/cyan]\n\n"
                    f"{metabase_instruction}"
                    f"[dim]To remove: [cyan]dango model remove {model_name.replace('.sql', '')}[/cyan][/dim]"
                )
            else:
                next_steps = (
                    f"[dim]Next Steps:[/dim]\n\n"
                    f"1. Edit your transformation:\n"
                    f"   • File: {model_path.relative_to(self.project_root)}\n"
                    f"   • Write SQL to transform data from staging tables\n"
                    f"   • Intermediate tables are building blocks for marts\n\n"
                    f"2. Run your model to create the table:\n"
                    f"   [cyan]dango run --select {model_name.replace('.sql', '')}[/cyan]\n"
                    f"   • Creates table in DuckDB\n"
                    f"   • [bold]AUTOMATED:[/bold] schema.yml auto-generated with all columns\n"
                    f"   [dim]• Use +model to run dependencies first: [cyan]--select +{model_name.replace('.sql', '')}[/cyan]\n"
                    f"   • Use model+ to run this + downstream: [cyan]--select {model_name.replace('.sql', '')}+[/cyan]\n"
                    f"   • Use +model+ for both: [cyan]--select +{model_name.replace('.sql', '')}+[/cyan][/dim]\n\n"
                    f"3. Add descriptions to your table:\n"
                    f"   • Open auto-generated: dbt/models/intermediate/schema.yml\n"
                    f"   • Fill in description: fields for table and columns\n"
                    f"   • Regenerate docs: [cyan]dango docs[/cyan]\n\n"
                    f"4. Use this table in marts models:\n"
                    f"   • Create marts with: [cyan]dango model add[/cyan]\n"
                    f"   • Reference this table: {{{{ ref('{model_name.replace('.sql', '')}') }}}}\n\n"
                    f"[dim]To remove: [cyan]dango model remove {model_name.replace('.sql', '')}[/cyan][/dim]"
                )

            console.print(
                Panel(
                    f"[green]✓ Model created successfully![/green]\n\n"
                    f"[bold]File:[/bold] {model_path.relative_to(self.project_root)}\n"
                    f"[bold]Layer:[/bold] {model_layer}\n"
                    f"[bold]Materialization:[/bold] {materialization}\n\n"
                    f"{next_steps}",
                    title="🎉 Success",
                    border_style="green",
                )
            )

            return model_path

        except KeyboardInterrupt:
            console.print("\n[yellow]Cancelled[/yellow]")
            return None

    def _ask_layer(self) -> str | None:
        """Ask which model layer"""
        console.print("\n[bold]Choose a layer:[/bold]")
        console.print("[dim]• Intermediate = Building blocks (used by other models)[/dim]")
        console.print("[dim]• Marts = Final tables (queried by analysts/dashboards)[/dim]\n")

        questions = [
            inquirer.List(
                "layer",
                message="Which layer?",
                choices=[
                    ("Intermediate - Building blocks used by other models", "intermediate"),
                    ("Marts - Final tables for analysts/dashboards", "marts"),
                ],
            )
        ]

        answers = inquirer.prompt(questions)
        return answers.get("layer") if answers else None

    def _ask_name(self, layer: str) -> str | None:
        """
        Ask for model name and enforce naming conventions

        Args:
            layer: Model layer (intermediate or marts)

        Returns:
            Model name with .sql extension, or None if cancelled
        """
        # Adjust prompt based on layer
        if layer == "intermediate":
            example = "customer_orders, revenue_summary"
            hint = "(will be prefixed with int_)"
        else:
            example = "customer_orders, revenue_by_month"
            hint = ""

        console.print(f"[cyan]e.g., {example} {hint}[/cyan]")
        questions = [
            inquirer.Text(
                "name",
                message="Model name",
                validate=lambda _, x: len(x) > 0 and x.replace("_", "").isalnum(),
            )
        ]

        answers = inquirer.prompt(questions)
        if not answers:
            return None

        name = answers["name"].strip().lower()

        # Remove any existing prefix to avoid double-prefixing
        if layer == "intermediate":
            if name.startswith("int_"):
                name = name[4:]  # Remove int_ prefix if user added it

        # Enforce naming convention for intermediate models
        if layer == "intermediate":
            name = f"int_{name}"
            console.print(f"[dim]→ Using naming convention: {name}.sql[/dim]\n")

        # Ensure .sql extension
        if not name.endswith(".sql"):
            name = f"{name}.sql"

        return name

    def _ask_description(self) -> str:
        """Ask for model description"""
        questions = [inquirer.Text("description", message="Description (optional)", default="")]

        answers = inquirer.prompt(questions)
        return answers.get("description", "").strip() if answers else ""

    def _ask_materialization(self, layer: str) -> str:
        """
        Ask for materialization strategy

        Args:
            layer: Model layer

        Returns:
            Materialization type (view or table)
        """
        # All models are tables for MVP (for Metabase visibility)
        # Views in DuckDB don't refresh properly in Metabase
        return "table"

    def _check_global_collision(self, name: str, current_layer: str) -> str | None:
        """
        Check if model name already exists in ANY layer

        Args:
            name: Model name (with .sql extension)
            current_layer: Layer being created in (to skip checking same layer)

        Returns:
            Existing model path (relative to project root) if collision found, None otherwise
        """
        # Check all layer directories
        layers_to_check = ["staging", "intermediate", "marts"]

        for layer in layers_to_check:
            if layer == current_layer:
                continue  # Skip current layer (will be checked later)

            layer_dir = self.models_dir / layer
            if not layer_dir.exists():
                continue

            # All layers use flat structure
            model_path = layer_dir / name
            if model_path.exists():
                return str(model_path.relative_to(self.project_root))

        return None

    def _create_model_file(
        self, layer: str, name: str, description: str, materialization: str
    ) -> Path | None:
        """
        Create the dbt model SQL file

        Args:
            layer: Model layer (intermediate or marts)
            name: Model name (with .sql extension)
            description: Model description
            materialization: Materialization type

        Returns:
            Path to created file, or None if file already exists
        """
        # Get layer directory
        layer_dir = self.models_dir / layer
        layer_dir.mkdir(parents=True, exist_ok=True)

        # Check for global collision first (across all layers)
        existing_path = self._check_global_collision(name, layer)
        if existing_path:
            console.print(f"\n[red]✗ Error:[/red] Model '{name}' already exists in another layer")
            console.print(f"[dim]Existing file: {existing_path}[/dim]\n")
            console.print("[yellow]Why this matters:[/yellow]")
            console.print("  • dbt requires unique model names across ALL layers")
            console.print("  • Model references use name only: {{ ref('model_name') }}")
            console.print("  • Duplicate names cause compilation errors\n")
            console.print("[yellow]Options:[/yellow]")
            console.print("  • Use a different name")
            console.print(
                f"  • Remove existing: [cyan]dango model remove {name.replace('.sql', '')}[/cyan]\n"
            )
            return None

        # Check if file already exists in same layer
        model_path = layer_dir / name
        if model_path.exists():
            console.print(f"\n[red]✗ Error:[/red] Model '{name}' already exists")
            console.print(f"[dim]File: {model_path.relative_to(self.project_root)}[/dim]\n")
            console.print("[yellow]Options:[/yellow]")
            console.print("  • Use a different name")
            console.print(
                f"  • Remove existing: [cyan]dango model remove {name.replace('.sql', '')}[/cyan]\n"
            )
            return None

        # Generate SQL content
        sql_content = self._generate_sql_template(
            layer=layer, name=name, description=description, materialization=materialization
        )

        # Write file
        with open(model_path, "w") as f:
            f.write(sql_content)

        return model_path

    def _generate_sql_template(
        self, layer: str, name: str, description: str, materialization: str
    ) -> str:
        """
        Generate SQL template content with comprehensive documentation

        Args:
            layer: Model layer
            name: Model name
            description: Model description
            materialization: Materialization type

        Returns:
            SQL file content
        """
        model_name = name.replace(".sql", "")
        timestamp = datetime.now().strftime("%Y-%m-%d")

        # Build header
        lines = [
            f"-- {model_name}",
            f"-- Created: {timestamp}",
        ]

        if description:
            lines.append(f"-- {description}")

        lines.append("")

        # Config block
        lines.append("{{ config(")
        lines.append(f"    materialized='{materialization}',")
        lines.append(f"    schema='{layer}'")
        lines.append(") }}")
        lines.append("")

        # Comprehensive reference guide
        lines.append("-- " + "=" * 76)
        lines.append("-- HOW TO REFERENCE TABLES IN dbt")
        lines.append("-- " + "=" * 76)
        lines.append("--")
        lines.append("-- 1. STAGING MODELS (auto-generated, in staging schema)")
        lines.append("--    {# {{ ref('stg_source_name') }} #}")
        lines.append("--    Example: FROM {# {{ ref('stg_stripe_customers') }} #}")
        lines.append("--")
        lines.append("--    These are auto-generated from your data sources.")
        lines.append("--    List available: dango models list")
        lines.append("--")
        lines.append("-- 2. INTERMEDIATE MODELS (in intermediate schema)")
        lines.append("--    {# {{ ref('int_model_name') }} #}")
        lines.append("--    Example: FROM {# {{ ref('int_customer_orders') }} #}")
        lines.append("--")
        lines.append("--    These are building blocks you create for reusable logic.")
        lines.append("--")
        lines.append("-- 3. MARTS MODELS (in marts schema, no prefix)")
        lines.append("--    {# {{ ref('model_name') }} #}")
        lines.append("--    Example: FROM {# {{ ref('customer_revenue') }} #}")
        lines.append("--")
        lines.append("--    These are final business tables for reporting/dashboards.")
        lines.append("--")
        lines.append("-- " + "=" * 76)
        lines.append("-- COMMON PATTERNS")
        lines.append("-- " + "=" * 76)
        lines.append("--")
        lines.append("-- PATTERN 1: Simple transformation (intermediate layer)")
        lines.append("-- SELECT")
        lines.append("--     customer_id,")
        lines.append("--     UPPER(customer_name) AS customer_name,")
        lines.append("--     total_orders")
        lines.append("-- FROM {# {{ ref('stg_customers') }} #}")
        lines.append("--")
        lines.append("-- PATTERN 2: Join multiple tables (intermediate or marts layer)")
        lines.append("-- SELECT")
        lines.append("--     c.customer_id,")
        lines.append("--     c.customer_name,")
        lines.append("--     COUNT(o.order_id) AS order_count,")
        lines.append("--     SUM(o.amount) AS total_revenue")
        lines.append("-- FROM {# {{ ref('stg_customers') }} #} c")
        lines.append("-- LEFT JOIN {# {{ ref('stg_orders') }} #} o")
        lines.append("--     ON c.customer_id = o.customer_id")
        lines.append("-- GROUP BY 1, 2")
        lines.append("--")
        lines.append("-- PATTERN 3: Build on intermediate models (marts layer)")
        lines.append("-- SELECT")
        lines.append("--     co.customer_id,")
        lines.append("--     co.customer_name,")
        lines.append("--     co.order_count,")
        lines.append("--     cr.total_revenue,")
        lines.append("--     cr.avg_order_value")
        lines.append("-- FROM {# {{ ref('int_customer_orders') }} #} co")
        lines.append("-- LEFT JOIN {# {{ ref('int_customer_revenue') }} #} cr")
        lines.append("--     ON co.customer_id = cr.customer_id")
        lines.append("--")
        lines.append("-- " + "=" * 76)
        lines.append("-- YOUR SQL STARTS HERE")
        lines.append("-- " + "=" * 76)
        lines.append("")
        lines.append("-- Replace this placeholder query with your actual transformation")
        lines.append("SELECT 1 as placeholder")
        lines.append("    -- Example transformations:")
        lines.append("    -- SELECT * FROM {{ ref('stg_customers') }}")
        lines.append("    -- SELECT * FROM {{ ref('int_customer_orders') }}")

        return "\n".join(lines) + "\n"

    def _regenerate_manifest(self) -> bool:
        """
        Regenerate dbt manifest.json so new model appears in Web UI

        Returns:
            True if successful, False otherwise
        """
        import subprocess

        try:
            # Run dbt parse to regenerate manifest
            result = subprocess.run(
                [
                    "dbt",
                    "parse",
                    "--project-dir",
                    str(self.dbt_dir),
                    "--profiles-dir",
                    str(self.dbt_dir),
                ],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] Model registered")
                return True
            else:
                console.print("[dim]ℹ Model won't appear in Web UI until first run[/dim]")
                return False

        except subprocess.TimeoutExpired:
            console.print("[dim]ℹ Model won't appear in Web UI until first run[/dim]")
            return False
        except Exception:
            console.print("[dim]ℹ Model won't appear in Web UI until first run[/dim]")
            return False


def add_model(project_root: Path) -> Path | None:
    """
    Run the model wizard

    Args:
        project_root: Path to project root

    Returns:
        Path to created model, or None if cancelled
    """
    wizard = ModelWizard(project_root)
    return wizard.run()
