"""
Generic Source Wizard

Metadata-driven wizard that works for all 27+ data sources.
Uses SOURCE_REGISTRY for display names, categories, and parameters.
"""

import os
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table
from rich.panel import Panel
import inquirer
from inquirer import themes

from dango.config.loader import load_config, save_config
from dango.config.models import DataSource, SourceType
from dango.ingestion.sources.registry import (
    SOURCE_REGISTRY,
    CATEGORIES,
    get_source_metadata,
    get_sources_by_category,
    get_all_categories,
    AuthType,
)
from dango.cli.env_helpers import (
    create_env_template,
    guide_env_setup,
)
from dango.oauth.router import (
    run_oauth_for_source,
    check_oauth_credentials_exist,
    OAUTH_PROVIDER_MAP,
)

console = Console()


class SourceWizard:
    """Generic wizard for adding data sources"""

    def __init__(self, project_root: Path):
        """
        Initialize wizard

        Args:
            project_root: Path to dango project root
        """
        self.project_root = project_root
        self.config_path = project_root / ".dango"
        self.sources_path = self.config_path / "sources.yml"
        self.env_file = project_root / ".env"
        self.secret_params = []  # Track secret parameters for .env setup

    def run(self) -> bool:
        """
        Run the source wizard

        Returns:
            True if source added successfully, False otherwise
        """
        try:
            console.print("\n[bold cyan]🍡 Dango Source Wizard[/bold cyan]\n")
            console.print("[dim]Press Ctrl+C at any time to abort (nothing saved until the end)[/dim]\n")

            # State machine for navigation with back button support
            category = None
            source_type = None
            metadata = None
            source_name = None
            params = None

            # Navigation states: category -> source -> name -> params -> save
            state = "category"

            while True:
                if state == "category":
                    # Step 1: Select category
                    category = self._select_category()
                    if not category:
                        return False  # User cancelled
                    state = "source"

                elif state == "source":
                    # Step 2: Select source from category
                    source_type = self._select_source(category)
                    if not source_type:
                        # User clicked back - go to category selection
                        state = "category"
                        continue

                    # Get source metadata
                    metadata = get_source_metadata(source_type)
                    if not metadata:
                        console.print(f"[red]❌ Source '{source_type}' not found in registry[/red]")
                        return False

                    # Show source info
                    self._show_source_info(metadata)

                    # Special handling for dlt_native sources (file-based config only)
                    if source_type == "dlt_native":
                        console.print(f"\n[yellow]⚠️  dlt_native sources must be configured manually[/yellow]\n")
                        console.print(f"[bold]This is an advanced feature for file-based configuration:[/bold]")
                        console.print(f"  1. Edit .dango/sources.yml manually")
                        console.print(f"  2. Add a source with type: dlt_native")
                        console.print(f"  3. Configure source_module, source_function, and function_kwargs")
                        console.print(f"\n[cyan]Example configuration:[/cyan]")
                        console.print(f"  sources:")
                        console.print(f"    - name: my_custom_source")
                        console.print(f"      type: dlt_native")
                        console.print(f"      dlt_native:")
                        console.print(f"        source_module: my_source")
                        console.print(f"        source_function: my_source_func")
                        console.print(f"        function_kwargs:")
                        console.print(f"          api_key_env: MY_API_KEY")
                        console.print(f"\n[dim]See docs/ADVANCED_USAGE.md for more examples[/dim]\n")

                        # Ask if user wants to go back
                        import inquirer
                        questions = [
                            inquirer.List(
                                "action",
                                message="What would you like to do?",
                                choices=["← Back to source selection", "Exit wizard"],
                            )
                        ]
                        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
                        if not answers or answers["action"] == "Exit wizard":
                            return False
                        else:
                            state = "source"
                            continue

                    state = "name"

                elif state == "name":
                    # Step 3: Collect source name
                    source_name = self._get_source_name(source_type, metadata)
                    if source_name == "← Back":
                        # Go back to source selection
                        state = "source"
                        continue
                    if not source_name:
                        return False  # User cancelled

                    # Step 3b: Check if OAuth setup is needed (inline flow)
                    # NOW we have source_name, so we can save instance-specific credentials
                    oauth_result = self._handle_oauth_setup(source_type, source_name, metadata)
                    if oauth_result == "back":
                        # User wants to go back - return to name prompt
                        continue
                    elif oauth_result == "cancel":
                        return False

                    state = "params"

                elif state == "params":
                    # Step 4: Collect parameters
                    params = self._collect_parameters(metadata, source_name)
                    if params == "← Back":
                        # Go back to source name
                        state = "name"
                        continue
                    if params is None:
                        return False  # User cancelled
                    # All inputs collected, break out of state machine
                    break

            # Step 6b: Create directory if this is a CSV source
            if source_type == "csv" and "directory" in params:
                directory_path = self.project_root / params["directory"]
                if not directory_path.exists():
                    directory_path.mkdir(parents=True, exist_ok=True)
                    console.print(f"[green]✅ Created directory: {params['directory']}[/green]")

            # Step 7: Create source config
            source_config = self._create_source_config(
                source_name, source_type, params, metadata
            )

            # Step 8: If secrets required, validate credentials FIRST (before saving)
            if self.secret_params:
                console.print(f"\n[bold]Setting up credentials...[/bold]")

                # Create .env template
                create_env_template(self.env_file, self.secret_params)
                console.print(f"[green]✅ Created .env template[/green]")

                # Guide user through credential setup with validation
                # Pass setup_guide for detailed instructions
                setup_guide = metadata.get("setup_guide", [])
                validated = guide_env_setup(
                    self.env_file,
                    self.secret_params,
                    source_name,
                    setup_guide
                )

                if not validated:
                    # Credentials not validated - don't save source config
                    console.print(f"\n[yellow]⚠️  Setup cancelled - credentials not validated[/yellow]")
                    console.print(f"\n[cyan]To retry:[/cyan]")
                    console.print(f"  dango source add")
                    return False

            # Step 9: Only save source config if validation passed or no secrets required
            self._save_source(source_config)
            console.print(f"\n[green]✅ Saved '{source_name}' to sources.yml[/green]")

            # Success messages based on whether secrets were required
            if self.secret_params:
                console.print(f"\n[green]✅ Source '{source_name}' fully configured![/green]")
                console.print(f"\n[cyan]Ready to sync:[/cyan]")
                console.print(f"  dango sync --source {source_name}")
            else:
                # No secrets required
                console.print(f"\n[green]✅ Source '{source_name}' added successfully![/green]")

                # Auto-validate configuration
                console.print(f"\n[dim]Validating configuration...[/dim]")
                from dango.config import ConfigLoader
                loader = ConfigLoader(self.project_root)
                is_valid, errors = loader.validate_config()

                if is_valid:
                    console.print("[green]✓[/green] Configuration valid")
                else:
                    console.print("[yellow]⚠️  Configuration warnings:[/yellow]")
                    for error in errors:
                        console.print(f"  • {error}")
                    console.print("[dim]Run 'dango config validate' to see details[/dim]")

                # CSV-specific instructions
                if source_type == "csv" and "directory" in params:
                    console.print(f"\n[bold cyan]What to do now:[/bold cyan]")
                    console.print(f"\n[bold]Option A: Use Web UI (recommended)[/bold]")
                    console.print(f"  1. Start platform: [cyan]dango start[/cyan]")
                    console.print(f"  2. Upload files via Web UI (sync happens automatically)")
                    console.print(f"  3. [dim](Optional)[/dim] Document tables: [cyan]dango docs[/cyan]")
                    console.print(f"\n[bold]Option B: Copy files manually[/bold]")
                    console.print(f"  1. Copy CSV files to: [cyan]{params['directory']}[/cyan]")
                    console.print(f"  2. Load data: [cyan]dango sync --source {source_name}[/cyan]")
                    console.print(f"     • Creates dbt staging models in dbt/models/staging/{source_name}/")
                    console.print(f"     • Creates documentation file: sources.yml")
                    console.print(f"  3. [dim](Optional)[/dim] Document tables: [cyan]dango docs[/cyan]")
                    console.print(f"\n[dim]Notes:[/dim]")
                    console.print(f"  • All files must have same columns (first row = headers)")
                    console.print(f"  • Change folder/filters → .dango/sources.yml")
                    console.print(f"  • Add column descriptions → dbt/models/staging/{source_name}/sources.yml")
                else:
                    console.print(f"\n[bold cyan]What to do now:[/bold cyan]")
                    console.print(f"  1. Load your data: [cyan]dango sync --source {source_name}[/cyan]")
                    console.print(f"     • This creates dbt staging models in dbt/models/staging/{source_name}/")
                    console.print(f"     • Documentation file created: dbt/models/staging/{source_name}/sources.yml")
                    console.print(f"  2. Document your tables (optional): Edit sources.yml to add descriptions")
                    console.print(f"     • Regenerate docs: [cyan]dango docs[/cyan]")
                    console.print(f"\n[dim]To customize later:[/dim]")
                    console.print(f"  • Change connection settings → .dango/sources.yml")
                    console.print(f"  • Update column descriptions → dbt/models/staging/{source_name}/sources.yml (created after first sync)")

            return True

        except KeyboardInterrupt:
            console.print("\n\n[yellow]Wizard cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]❌ Error: {e}[/red]")
            return False

    def _select_category(self) -> Optional[str]:
        """Select source category"""
        categories = get_all_categories()

        # Create display with counts and examples
        choices = []
        for category in categories:
            sources_in_category = get_sources_by_category(category)
            # Filter to only v0-supported sources
            available = [s for s in sources_in_category if s in SOURCE_REGISTRY and SOURCE_REGISTRY[s].get("supported_in_v0", False)]
            count = len(available)

            # Skip categories with no v0-supported sources
            if count == 0:
                continue

            # Show first 2 sources as examples
            examples = []
            for source in available[:2]:
                metadata = get_source_metadata(source)
                examples.append(metadata.get("display_name", source))

            example_text = ", ".join(examples)
            if len(available) > 2:
                example_text += ", ..."

            choices.append(f"{category} ({count}) - {example_text}")

        questions = [
            inquirer.List(
                "category",
                message="Select source category",
                choices=choices + ["← Back"],
                carousel=True,
            )
        ]

        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers or answers["category"] == "← Back":
            return None

        # Extract category name (remove count and examples)
        return answers["category"].split(" (")[0]

    def _select_source(self, category: str) -> Optional[str]:
        """Select specific source from category"""
        sources = get_sources_by_category(category)

        # Filter to only v0-supported sources
        available_sources = [s for s in sources if s in SOURCE_REGISTRY and SOURCE_REGISTRY[s].get("supported_in_v0", False)]

        if not available_sources:
            console.print(f"[yellow]No sources available in {category}[/yellow]")
            return None

        # Create choices with display names
        choices = []
        for source_type in available_sources:
            metadata = get_source_metadata(source_type)
            display_name = metadata.get("display_name", source_type)
            choices.append((display_name, source_type))

        # Sort alphabetically by display name
        choices.sort(key=lambda x: x[0])

        questions = [
            inquirer.List(
                "source",
                message=f"Select source from {category}",
                choices=[c[0] for c in choices] + ["← Back"],
                carousel=True,
            )
        ]

        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers or answers["source"] == "← Back":
            return None

        # Find source_type from display name
        for display_name, source_type in choices:
            if display_name == answers["source"]:
                return source_type

        return None

    def _show_source_info(self, metadata: Dict[str, Any]) -> None:
        """Display source information"""
        console.print(f"\n[bold]{metadata.get('display_name')}[/bold]")
        console.print(f"{metadata.get('description')}\n")

        if metadata.get("cost_warning"):
            console.print(f"[yellow]💰 {metadata['cost_warning']}[/yellow]\n")

        # Skip setup_guide - instructions shown at end after config

        if metadata.get("docs_url"):
            console.print(f"[dim]📚 Docs: {metadata['docs_url']}[/dim]\n")

    def _handle_oauth_setup(self, source_type: str, source_name: str, metadata: Dict[str, Any]) -> Optional[str]:
        """
        Handle OAuth setup for sources that require it.

        This runs inline during source wizard AFTER getting source name,
        so we can save instance-specific credentials for multi-account setups.

        Args:
            source_type: Source type key (e.g., "facebook_ads")
            source_name: Source instance name (e.g., "facebook_us")
            metadata: Source metadata from registry

        Returns:
            None if OAuth setup successful or not needed
            "back" if user wants to go back
            "cancel" if user cancelled
        """
        # Check if this source requires OAuth
        auth_type = metadata.get("auth_type")
        if auth_type != AuthType.OAUTH:
            # Not an OAuth source, continue
            return None

        # Check if this source has an OAuth provider configured
        if source_type not in OAUTH_PROVIDER_MAP:
            # OAuth marked in registry but no provider - warn and continue
            console.print(f"[yellow]⚠️  OAuth required but provider not yet implemented for {source_type}[/yellow]")
            console.print(f"[yellow]   You'll need to configure credentials manually in .dlt/secrets.toml[/yellow]\n")
            return None

        # Check if credentials already exist for THIS SOURCE INSTANCE
        creds_exist = check_oauth_credentials_exist(source_type, source_name, self.project_root)

        if creds_exist:
            console.print(f"[green]✓ OAuth credentials already configured for {source_name}[/green]")
            console.print(f"[dim]  Credentials found in .dlt/secrets.toml[/dim]")
            console.print(f"[dim]  To re-authenticate: dango auth {source_type}[/dim]\n")
            return None

        # No credentials - prompt user to set up OAuth
        console.print(f"[yellow]⚠️  OAuth authentication required[/yellow]")
        console.print(f"[cyan]This source requires OAuth credentials to access your data.[/cyan]\n")

        # Ask if user wants to set up OAuth now
        questions = [
            inquirer.List(
                "oauth_action",
                message="How would you like to proceed?",
                choices=[
                    "Set up OAuth now (recommended)",
                    "Skip for now (configure manually later)",
                    "← Back to source selection",
                ],
                carousel=True,
            )
        ]

        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return "cancel"

        action = answers["oauth_action"]

        if action == "← Back to source selection":
            return "back"

        elif action == "Skip for now (configure manually later)":
            console.print(f"\n[yellow]⚠️  Skipping OAuth setup[/yellow]")
            console.print(f"[cyan]To authenticate later, run:[/cyan]")
            console.print(f"  dango auth {source_type}")
            console.print(f"\n[dim]You can still configure this source, but you won't be able to sync")
            console.print(f"until you set up OAuth credentials.[/dim]\n")
            return None

        else:  # "Set up OAuth now"
            console.print(f"\n[bold]Starting OAuth setup for {metadata.get('display_name')}...[/bold]\n")

            # Run OAuth flow with source_name for instance-specific credentials
            # This allows multiple accounts: facebook_us, facebook_eu, etc.
            success = run_oauth_for_source(source_type, source_name, self.project_root)

            if success:
                console.print(f"\n[green]✅ OAuth credentials configured successfully![/green]")
                console.print(f"[dim]  Credentials saved to .dlt/secrets.toml[/dim]\n")
                return None
            else:
                console.print(f"\n[red]❌ OAuth setup failed[/red]")
                console.print(f"[yellow]You can try again later with: dango auth {source_type}[/yellow]\n")

                # Ask if user wants to continue anyway
                continue_anyway = Confirm.ask(
                    "Continue configuring source without OAuth credentials?",
                    default=False
                )

                if continue_anyway:
                    console.print(f"[yellow]⚠️  Continuing without credentials[/yellow]")
                    console.print(f"[yellow]   You won't be able to sync until you authenticate[/yellow]\n")
                    return None
                else:
                    return "back"

    def _get_source_name(self, source_type_key: str, metadata: Dict[str, Any]) -> Optional[str]:
        """Get unique source name from user with contextual help

        Args:
            source_type_key: Source type key from registry (e.g., "stripe", "shopify")
            metadata: Source metadata from registry

        Returns:
            Full source name (auto-prefixed for multi-resource sources)
        """
        source_type_display = metadata.get("display_name", "source")
        is_multi_resource = metadata.get("multi_resource", False)

        while True:
            # Show contextual help based on source type
            if is_multi_resource:
                console.print(f"\n[bold]Name this {source_type_display} connection:[/bold]")
                console.print(f"[cyan]Enter a suffix to identify this connection (e.g., 'test', 'prod', 'staging')[/cyan]")
                console.print(f"[cyan]Full source name will be: {source_type_key}_<your_input>[/cyan]")
                console.print(f"[dim]Examples: test, prod, staging, us, eu, company_name[/dim]")
            else:
                console.print(f"\n[bold]Name this data source:[/bold]")
                console.print(f"[cyan]Use underscores, not spaces - e.g., 'orders', 'customer_data'[/cyan]")

            console.print("[dim]Type 'back' to return to source selection[/dim]")

            questions = [
                inquirer.Text(
                    "name",
                    message="Connection suffix" if is_multi_resource else "Source name",
                )
            ]

            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None

            user_input = answers["name"].strip()

            # Check if user wants to go back
            if user_input.lower() == "back":
                return "← Back"

            # Validate name format
            if not user_input or not user_input.replace("_", "").replace("-", "").isalnum():
                console.print(f"[yellow]⚠️  Invalid format. Use letters, numbers, underscores, and hyphens only.[/yellow]")
                continue

            # Build final source name
            if is_multi_resource:
                # Auto-prefix with source type
                final_source_name = f"{source_type_key}_{user_input}"
            else:
                # Use as-is for single-resource sources
                final_source_name = user_input

            # Check if final name already exists
            if self._source_name_exists(final_source_name):
                console.print(f"[yellow]⚠️  Source '{final_source_name}' already exists. Choose a different name.[/yellow]")
                continue

            # Show what will be created
            if is_multi_resource:
                console.print(f"\n[cyan]✓ Will create source: '{final_source_name}'[/cyan]")
                console.print(f"  [dim]Raw schema: raw_{final_source_name}[/dim]")
                console.print(f"  [dim]Raw tables: raw_{final_source_name}.charge, raw_{final_source_name}.customer, etc.[/dim]")
                console.print(f"  [dim]Staging models: stg_{final_source_name}__charge, stg_{final_source_name}__customer, etc.[/dim]")
                console.print(f"  [dim]Sync command: dango sync --source {final_source_name}[/dim]\n")
            else:
                console.print(f"\n[cyan]✓ Will create source: '{final_source_name}'[/cyan]\n")

            return final_source_name

    def _source_name_exists(self, name: str) -> bool:
        """Check if source name already exists in config"""
        if not self.sources_path.exists():
            return False

        config = load_config(self.project_root)
        return any(s.name == name for s in config.sources.sources)

    def _collect_parameters(self, metadata: Dict[str, Any], source_name: str) -> Optional[Dict[str, Any]]:
        """Collect required and optional parameters from user"""
        params = {}
        source_type = metadata.get("display_name", "source")

        # Collect required parameters
        required_params = metadata.get("required_params", [])
        if required_params:
            console.print("[bold]Required Parameters:[/bold]")
            console.print("[dim]Type 'back' in any field to return to source name[/dim]")
            for param in required_params:
                # Inject source_name into directory default for CSV sources
                if param["name"] == "directory" and param.get("default") == "data/uploads":
                    param = param.copy()  # Don't modify registry
                    param["default"] = f"data/uploads/{source_name}"

                value = self._prompt_parameter(param, source_name, source_type, metadata, required=True)
                if value is None:
                    return None
                # Check if user wants to go back
                if isinstance(value, str) and value.lower() == "back":
                    return "← Back"
                params[param["name"]] = value

        # Ask optional parameters directly (no meta-question)
        optional_params = metadata.get("optional_params", [])
        if optional_params:
            console.print("\n[bold]Optional settings[/bold] [dim](press Enter to use defaults, edit .dango/sources.yml to change later)[/dim]")
            for param in optional_params:
                value = self._prompt_parameter(param, source_name, source_type, metadata, required=False)
                # Check if user wants to go back
                if isinstance(value, str) and value.lower() == "back":
                    return "← Back"
                if value is not None:
                    params[param["name"]] = value

        return params

    def _prompt_parameter(
        self, param: Dict[str, Any], source_name: str, source_type: str, metadata: Dict[str, Any], required: bool = True
    ) -> Optional[Any]:
        """Prompt user for a single parameter

        Args:
            param: Parameter configuration from registry
            source_name: Full name of the source being configured (e.g., "stripe_test")
            source_type: Display name of source type (e.g., "Stripe")
            metadata: Source metadata from registry (contains multi_resource flag)
            required: Whether this parameter is required
        """
        param_name = param["name"]
        param_type = param.get("type", "string")
        prompt = param.get("prompt", param_name)
        help_text = param.get("help", "")
        default = param.get("default")

        # Show help text if available (important context for user)
        if help_text:
            console.print(f"  [cyan]{help_text}[/cyan]")

        # Different prompt types based on parameter type
        if param_type == "secret" or param_name.endswith("_env"):
            # Secret/env var parameter - generate unique env var name per source instance
            # This allows multiple sources of same type with different credentials

            # Get base env var from registry (e.g., "STRIPE_API_KEY")
            base_env_var = param.get("env_var", param_name.upper())

            # For multi-resource sources, source_name is auto-prefixed (e.g., "stripe_test")
            # We need to extract just the suffix for env var generation
            # Example: "stripe_test" → extract "test" → generate "STRIPE_TEST_API_KEY"
            is_multi_resource = metadata.get("multi_resource", False)

            if is_multi_resource:
                # Extract suffix from auto-prefixed source name
                # source_name format: "{source_type_key}_{suffix}"
                # We need to find the first underscore and take everything after it
                parts = source_name.split("_", 1)
                if len(parts) == 2:
                    name_suffix = parts[1]  # e.g., "test" from "stripe_test"
                else:
                    # Fallback if no underscore found (shouldn't happen)
                    name_suffix = source_name
            else:
                # For single-resource sources, use full source_name
                name_suffix = source_name

            # Generate unique env var by injecting name suffix
            # Examples:
            #   test (from stripe_test) + STRIPE_API_KEY → STRIPE_TEST_API_KEY
            #   prod (from stripe_prod) + STRIPE_API_KEY → STRIPE_PROD_API_KEY
            #   us (from shopify_us) + SHOPIFY_ACCESS_TOKEN → SHOPIFY_US_ACCESS_TOKEN

            # Extract the prefix (source type) from base env var
            # STRIPE_API_KEY → STRIPE, SHOPIFY_ACCESS_TOKEN → SHOPIFY
            if "_" in base_env_var:
                prefix = base_env_var.split("_")[0]
                suffix = "_".join(base_env_var.split("_")[1:])
                # Insert name suffix between prefix and suffix
                env_var = f"{prefix}_{name_suffix.upper().replace('-', '_')}_{suffix}"
            else:
                # Fallback: just append name suffix
                env_var = f"{base_env_var}_{name_suffix.upper().replace('-', '_')}"

            # Check if env var already exists in .env
            env_exists = False
            if self.env_file.exists():
                env_content = self.env_file.read_text()
                for line in env_content.split('\n'):
                    line = line.strip()
                    if line and not line.startswith('#'):
                        if '=' in line:
                            key, value = line.split('=', 1)
                            if key.strip() == env_var and value.strip():
                                env_exists = True
                                break

            # Store secret metadata for .env template creation (only if not already set)
            if not env_exists:
                secret_metadata = {
                    'name': env_var,
                    'display_name': param.get('prompt', env_var),
                    'help': help_text or param.get('help', ''),
                    'format': param.get('format', ''),
                    'example': param.get('example', ''),
                    'source_name': source_name,  # Track which source this key is for
                    'source_type': source_type,
                }
                self.secret_params.append(secret_metadata)
                console.print(f"  [cyan]→ Credential for '{source_name}' will be: {env_var}[/cyan]")
                console.print(f"    [dim]You'll set this value in .env file[/dim]")
            else:
                console.print(f"  [green]✓ Already configured in .env: {env_var}[/green]")
                console.print(f"    [yellow]⚠️  This will be reused for '{source_name}'[/yellow]")

            return env_var

        elif param_type == "boolean" or param_type == "bool":
            questions = [
                inquirer.Confirm(
                    param_name,
                    message=prompt,
                    default=default if default is not None else False,
                )
            ]

        elif param_type == "choice":
            choices = param.get("choices", [])
            questions = [
                inquirer.List(
                    param_name,
                    message=prompt,
                    choices=choices + (["Skip"] if not required else []),
                    default=default,
                )
            ]

        elif param_type == "multiselect":
            choices = param.get("choices", [])
            questions = [
                inquirer.Checkbox(
                    param_name,
                    message=f"{prompt} (Space to select/deselect, Enter to continue)",
                    choices=choices,
                    default=default if default else [],
                )
            ]

        elif param_type == "date":
            # Date parameter - calculate actual date if default is None
            if default is None:
                # Use 30 days ago as default (same as old "30_days_ago" intent)
                calculated_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
                default_display = calculated_date
            else:
                default_display = str(default)

            questions = [
                inquirer.Text(
                    param_name,
                    message=prompt + (" (optional)" if not required else ""),
                    default=default_display,
                )
            ]

        else:
            # String, number, path, etc.
            questions = [
                inquirer.Text(
                    param_name,
                    message=prompt + (" (optional)" if not required else ""),
                    default=str(default) if default is not None else None,
                )
            ]

        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return None  # User cancelled (Ctrl+C) - always abort

        value = answers[param_name]

        # Skip if user chose to skip optional param
        if value == "Skip" and not required:
            return None

        # Return None for empty optional params
        if not required and value == "":
            return None

        # Show incremental loading education for start_date parameters
        if param_name == "start_date" and value:
            console.print("\n[cyan]ℹ️  About Incremental Loading:[/cyan]")
            console.print("  • start_date is only used for the FIRST sync")
            console.print("  • Future syncs load NEW data since last run")
            console.print("  • Cursor tracks when record was CREATED, not event date")
            console.print("  • Example: Dec 31 order might have created=Jan 1")
            console.print("\n[yellow]💡 Tip: Set start_date 7-14 days earlier to catch late data[/yellow]\n")

        return value

    def _create_source_config(
        self,
        source_name: str,
        source_type: str,
        params: Dict[str, Any],
        metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create source configuration dictionary"""
        config = {
            "name": source_name,
            "type": source_type,
            "enabled": True,
            "description": f"{metadata.get('display_name')} - added via wizard",
        }

        # Add type-specific config
        if params:
            # Convert source_type to config field name (e.g., "facebook_ads" -> "facebook_ads")
            config[source_type] = params

        return config

    def _save_source(self, source_config: Dict[str, Any]) -> None:
        """Save source to sources.yml"""
        config = load_config(self.project_root)

        # Add new source
        config.sources.sources.append(DataSource(**source_config))

        # Save
        save_config(config, self.project_root)


def add_source(project_root: Path) -> bool:
    """
    Run source wizard to add a new data source

    Args:
        project_root: Path to project root

    Returns:
        True if successful, False otherwise
    """
    wizard = SourceWizard(project_root)
    return wizard.run()
