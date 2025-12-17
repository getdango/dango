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
from dango.oauth.storage import OAuthStorage

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
            source_type = None
            metadata = None
            source_name = None
            params = None

            # Navigation states: source -> name -> params -> save
            state = "source"

            while True:
                if state == "source":
                    # Step 1: Select source (flat list, no categories)
                    source_type = self._select_source_flat()
                    if not source_type:
                        return False  # User cancelled

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
                    params = self._collect_parameters(source_type, metadata, source_name)
                    if params == "← Back":
                        # Go back to source name
                        state = "name"
                        continue
                    if params is None:
                        return False  # User cancelled
                    # All inputs collected, break out of state machine
                    break

            # Step 6: Write default_config to .dlt/config.toml (for stability across upgrades)
            if metadata.get("default_config"):
                self._write_config_template(source_type, metadata)

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

    def _select_source_flat(self) -> Optional[str]:
        """Select source from flat list (no categories)"""
        # Get all v0-supported sources
        all_sources = []
        for source_type, source_meta in SOURCE_REGISTRY.items():
            if source_meta.get("supported_in_v0", False):
                display_name = source_meta.get("display_name", source_type)
                all_sources.append((display_name, source_type))

        if not all_sources:
            console.print("[yellow]No sources available[/yellow]")
            return None

        # Sort alphabetically by display name
        all_sources.sort(key=lambda x: x[0])

        # Create choices list
        choices = [s[0] for s in all_sources]

        questions = [
            inquirer.List(
                "source",
                message="Select data source",
                choices=choices + ["← Cancel"],
                carousel=True,
            )
        ]

        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers or answers["source"] == "← Cancel":
            return None

        # Find source_type from display name
        for display_name, source_type in all_sources:
            if display_name == answers["source"]:
                return source_type

        return None

    def _select_category(self) -> Optional[str]:
        """Select source category (deprecated - kept for reference)"""
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

        With dlt best practice, credentials are stored directly at
        sources.{source_type}.credentials.* - one credential per source type.

        Args:
            source_type: Source type key (e.g., "facebook_ads", "google_ads")
            source_name: Source instance name (not used - credentials are per source type)
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

        # Check for existing OAuth credentials for this source type
        oauth_storage = OAuthStorage(self.project_root)
        existing_cred = oauth_storage.get(source_type)

        if existing_cred:
            # Credentials exist for this source type
            if existing_cred.is_expired():
                console.print(f"[red]⚠️  OAuth credentials for {source_type} have expired[/red]")
                console.print(f"[yellow]Re-authenticate with: dango auth {source_type}[/yellow]\n")

                questions = [
                    inquirer.List(
                        "oauth_action",
                        message="How would you like to proceed?",
                        choices=[
                            "Re-authenticate now",
                            "Continue anyway (sync will fail)",
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
                elif action == "Continue anyway (sync will fail)":
                    return None
                # Fall through to re-authenticate

            elif existing_cred.is_expiring_soon():
                days_left = existing_cred.days_until_expiry()
                console.print(f"[yellow]⚠️  OAuth credentials expire in {days_left} days[/yellow]")
                console.print(f"[green]✓ Using: {existing_cred.account_info}[/green]\n")
                return None

            else:
                # Valid credentials exist
                console.print(f"[green]✓ OAuth credentials found: {existing_cred.account_info}[/green]\n")
                return None

        # No existing credentials - prompt to set up new OAuth
        console.print(f"[yellow]⚠️  OAuth authentication required[/yellow]")
        console.print(f"[cyan]This source requires OAuth credentials to access your data.[/cyan]\n")

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

        # "Set up OAuth now" - run OAuth flow
        console.print(f"\n[bold]Starting OAuth setup for {metadata.get('display_name')}...[/bold]\n")

        success = run_oauth_for_source(source_type, source_name, self.project_root)

        if success:
            console.print(f"\n[green]✅ OAuth credentials configured successfully![/green]")
            console.print(f"[dim]  Credentials saved to .dlt/secrets.toml[/dim]\n")
            return None
        else:
            console.print(f"\n[red]❌ OAuth setup failed[/red]")
            console.print(f"[yellow]You can try again later with: dango auth {source_type}[/yellow]\n")

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
            Full source name
        """
        source_type_display = metadata.get("display_name", "source")

        while True:
            # Consistent naming prompt for all source types
            console.print(f"\n[bold]Name this {source_type_display} source:[/bold]")
            console.print(f"[cyan]Use lowercase with underscores (e.g., 'my_sales_data', 'prod_analytics')[/cyan]")
            console.print("[dim]Type 'back' to return to source selection[/dim]")

            questions = [
                inquirer.Text(
                    "name",
                    message="Source name",
                )
            ]

            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None

            user_input = answers["name"].strip().lower()

            # Check if user wants to go back
            if user_input == "back":
                return "← Back"

            # Validate name format
            if not user_input or not user_input.replace("_", "").isalnum():
                console.print(f"[yellow]⚠️  Invalid format. Use letters, numbers, and underscores only (no hyphens).[/yellow]")
                continue

            # Use name as-is (no auto-prefixing)
            final_source_name = user_input

            # Check if final name already exists
            if self._source_name_exists(final_source_name):
                console.print(f"[yellow]⚠️  Source '{final_source_name}' already exists. Choose a different name.[/yellow]")
                continue

            # Show what will be created (all sources use raw_{source_name} schema)
            console.print(f"\n[green]✓ Source name: '{final_source_name}'[/green]")
            console.print(f"  [dim]Raw schema: raw_{final_source_name}[/dim]")
            console.print(f"  [dim]Staging models: stg_{final_source_name}__<table>[/dim]")

            return final_source_name

    def _source_name_exists(self, name: str) -> bool:
        """Check if source name already exists in config"""
        if not self.sources_path.exists():
            return False

        config = load_config(self.project_root)
        return any(s.name == name for s in config.sources.sources)

    def _is_credential_param(self, param: Dict[str, Any], source_type: str) -> bool:
        """Check if a parameter is a credential/secret that should be skipped when using OAuth

        Args:
            param: Parameter configuration from registry
            source_type: Source type key (e.g., "facebook_ads", "google_ads")
        """
        param_name = param.get("name", "").lower()
        param_type = param.get("type", "")

        # Check if it's a secret type
        if param_type == "secret":
            return True

        # Check common credential parameter name patterns
        credential_patterns = [
            "credentials", "credential", "access_token", "api_key", "secret",
            "_env",  # Parameters ending in _env are typically env var references
        ]

        for pattern in credential_patterns:
            if pattern in param_name:
                return True

        # Source-specific credential parameters that are collected during OAuth
        # These are stored in .dlt/secrets.toml by the OAuth provider
        oauth_collected_params = {
            "facebook_ads": ["account_id"],  # Facebook OAuth collects account_id
            "google_ads": ["customer_id"],   # Google Ads OAuth collects customer_id
            "shopify": ["shop_url"],         # Shopify OAuth collects shop_url
        }

        if source_type in oauth_collected_params:
            if param_name in oauth_collected_params[source_type]:
                return True

        return False

    def _collect_parameters(self, source_type_key: str, metadata: Dict[str, Any], source_name: str) -> Optional[Dict[str, Any]]:
        """Collect required and optional parameters from user

        Args:
            source_type_key: Source type key from registry (e.g., "facebook_ads")
            metadata: Source metadata from registry
            source_name: Name for this source instance
        """
        params = {}
        source_type_display = metadata.get("display_name", "source")

        # Collect required parameters
        required_params = metadata.get("required_params", [])
        if required_params:
            console.print("[bold]Required Parameters:[/bold]")
            console.print("[dim]Type 'back' in any field to return to source name[/dim]")

            # Check if OAuth credentials exist for this source type
            oauth_storage = OAuthStorage(self.project_root)
            has_oauth = oauth_storage.exists(source_type_key)

            for param in required_params:
                # Skip credential parameters if OAuth credentials exist
                # OAuth credentials are stored in .dlt/secrets.toml at sources.{type}.credentials.*
                if has_oauth and self._is_credential_param(param, source_type_key):
                    console.print(f"  [green]✓ {param.get('prompt', param['name'])}: Using OAuth credentials[/green]")
                    continue

                # Inject source_name into directory default for CSV sources
                if param["name"] == "directory" and param.get("default") == "data/uploads":
                    param = param.copy()  # Don't modify registry
                    param["default"] = f"data/uploads/{source_name}"

                value = self._prompt_parameter(param, source_name, source_type_display, metadata, required=True)
                if value is None:
                    return None
                # Check if user wants to go back
                if isinstance(value, str) and value.lower() == "back":
                    return "← Back"
                params[param["name"]] = value

                # Store spreadsheet ID for sheet_selector type to use
                if param["name"] == "spreadsheet_url_or_id":
                    self._current_spreadsheet_id = value

        # Ask optional parameters directly (no meta-question)
        optional_params = metadata.get("optional_params", [])
        if optional_params:
            console.print("\n[bold]Optional settings[/bold] [dim](press Enter to use defaults, edit .dango/sources.yml to change later)[/dim]")
            for param in optional_params:
                value = self._prompt_parameter(param, source_name, source_type_display, metadata, required=False)
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
            metadata: Source metadata from registry
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

            # Use full source_name to generate unique env var
            name_suffix = source_name

            # Generate unique env var by injecting name suffix
            # Examples:
            #   stripe_test + STRIPE_API_KEY → STRIPE_STRIPE_TEST_API_KEY
            #   my_store + SHOPIFY_ACCESS_TOKEN → SHOPIFY_MY_STORE_ACCESS_TOKEN

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

        elif param_type == "sheet_selector":
            # Special type for Google Sheets: fetch sheets from API and show multi-select
            # Requires spreadsheet_url_or_id to already be collected
            try:
                sheets = self._fetch_google_sheets(source_name)
            except Exception as e:
                # OAuth authentication failed - abort wizard
                console.print(f"\n[red]Cannot continue without valid OAuth credentials.[/red]")
                console.print(f"[yellow]Please re-authenticate first, then try again.[/yellow]")
                return None

            if sheets is None:
                # No OAuth configured yet - fall back to manual entry
                console.print(f"[yellow]⚠️  No OAuth configured. Enter sheet names manually.[/yellow]")
                questions = [
                    inquirer.Text(
                        param_name,
                        message="Sheet/tab names (comma-separated)",
                        default="Sheet1",
                    )
                ]
                answers = inquirer.prompt(questions, theme=themes.GreenPassion())
                if not answers:
                    return None
                # Parse comma-separated input into list
                value = answers[param_name]
                return [s.strip() for s in value.split(",") if s.strip()]

            if not sheets:
                console.print(f"[yellow]⚠️  No sheets found in spreadsheet[/yellow]")
                return None

            # Loop until user confirms selection
            while True:
                # Show clear instructions before the checkbox
                console.print(f"\n[bold]Select sheets to load:[/bold]")
                console.print(f"[cyan]  ↑/↓  Navigate    Space  Select/deselect    Enter  Confirm[/cyan]\n")

                # Show checkbox for sheet selection
                questions = [
                    inquirer.Checkbox(
                        param_name,
                        message="Sheets",
                        choices=sheets,
                        default=[sheets[0]] if sheets else [],  # Default to first sheet
                    )
                ]
                answers = inquirer.prompt(questions, theme=themes.GreenPassion())
                if not answers:
                    return None

                selected = answers[param_name]
                if not selected:
                    console.print(f"[yellow]⚠️  You must select at least one sheet[/yellow]")
                    continue

                # Show selection and ask for confirmation
                console.print(f"\n[cyan]Selected {len(selected)} sheet(s):[/cyan]")
                for sheet in selected:
                    console.print(f"  • {sheet}")

                confirm_questions = [
                    inquirer.List(
                        "action",
                        message="Confirm selection?",
                        choices=[
                            ("Yes, continue", "confirm"),
                            ("No, reselect sheets", "reselect"),
                        ],
                    )
                ]
                confirm_answers = inquirer.prompt(confirm_questions, theme=themes.GreenPassion())
                if not confirm_answers:
                    return None

                if confirm_answers["action"] == "confirm":
                    console.print(f"  [green]✓ {len(selected)} sheet(s) selected[/green]")
                    return selected
                # Otherwise loop back to reselect

        elif param_type == "date":
            # Date parameter - leave blank if no default specified
            default_display = str(default) if default else None

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

    def _fetch_google_sheets(self, source_name: str) -> Optional[List[str]]:
        """
        Fetch sheet/tab names from a Google Spreadsheet using OAuth credentials.

        Args:
            source_name: Source name (used to find OAuth credentials)

        Returns:
            List of sheet names, or None if failed
        """
        try:
            from googleapiclient.discovery import build
            from google.oauth2.credentials import Credentials

            # Get OAuth credentials for Google Sheets
            oauth_storage = OAuthStorage(self.project_root)
            cred = oauth_storage.get("google_sheets")

            if not cred:
                console.print(f"[yellow]No Google Sheets OAuth credentials found[/yellow]")
                console.print(f"[dim]Run 'dango auth google_sheets' first[/dim]")
                return None

            # Get credentials from the OAuthCredential object
            tokens = cred.credentials
            if not tokens:
                console.print(f"[yellow]Could not get OAuth tokens[/yellow]")
                return None

            # Get scopes from metadata (saved during OAuth authentication)
            scopes = cred.metadata.get("scopes", []) if cred.metadata else []

            # Debug: Check what we have
            if not tokens.get("refresh_token"):
                console.print(f"[red]Error: No refresh token found in credentials[/red]")
                console.print(f"[dim]This usually means OAuth wasn't completed properly[/dim]")
                console.print(f"[cyan]Run: dango auth google_sheets[/cyan]")
                return None

            if not tokens.get("client_id") or not tokens.get("client_secret"):
                console.print(f"[red]Error: Missing client_id or client_secret[/red]")
                console.print(f"[dim]OAuth configuration is incomplete[/dim]")
                console.print(f"[cyan]Run: dango auth google_sheets[/cyan]")
                return None

            credentials = Credentials(
                token=None,  # We use refresh_token to get a new access_token
                refresh_token=tokens.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=tokens.get("client_id"),
                client_secret=tokens.get("client_secret"),
                scopes=scopes,
            )

            # Refresh credentials to get a new access token
            from google.auth.transport.requests import Request
            try:
                credentials.refresh(Request())
            except Exception as refresh_error:
                console.print(f"[red]Failed to refresh OAuth token[/red]")
                console.print(f"[dim]Details: {refresh_error}[/dim]")
                console.print(f"[dim]Error type: {type(refresh_error).__name__}[/dim]")
                # Re-raise so the outer try-except can handle it
                raise

            # Build Sheets API service
            service = build('sheets', 'v4', credentials=credentials, cache_discovery=False)

            # Get spreadsheet ID from the collected params
            # This is a bit tricky since we're in the middle of collecting params
            # We need to access the params collected so far
            # The spreadsheet_url_or_id should already be collected before range_names
            spreadsheet_id = getattr(self, '_current_spreadsheet_id', None)

            if not spreadsheet_id:
                console.print(f"[yellow]Spreadsheet ID not yet collected[/yellow]")
                return None

            # Extract ID from URL if needed
            if "docs.google.com" in spreadsheet_id:
                # URL format: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
                import re
                match = re.search(r'/d/([a-zA-Z0-9-_]+)', spreadsheet_id)
                if match:
                    spreadsheet_id = match.group(1)

            # Fetch spreadsheet metadata
            console.print(f"[dim]Fetching sheets from spreadsheet...[/dim]")
            result = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

            # Extract sheet names
            sheets = result.get('sheets', [])
            sheet_names = [sheet['properties']['title'] for sheet in sheets]

            # Check each sheet for data (need at least 2 rows: header + 1 data row)
            # Fetch first 2 rows of each sheet to determine if empty
            console.print(f"[dim]Checking for empty sheets...[/dim]")
            non_empty_sheets = []
            empty_sheets = []

            for sheet_name in sheet_names:
                try:
                    # Fetch just the first 2 rows to check if sheet has data
                    range_check = f"'{sheet_name}'!A1:Z2"
                    data_result = service.spreadsheets().values().get(
                        spreadsheetId=spreadsheet_id,
                        range=range_check
                    ).execute()

                    values = data_result.get('values', [])
                    # Sheet needs at least 2 rows (header + data) and first row needs content
                    if len(values) >= 2 and len(values[0]) > 0:
                        non_empty_sheets.append(sheet_name)
                    elif len(values) == 1 and len(values[0]) > 0:
                        # Has header but no data - still empty for our purposes
                        empty_sheets.append(sheet_name)
                    else:
                        empty_sheets.append(sheet_name)
                except Exception:
                    # If we can't check, assume it's non-empty to be safe
                    non_empty_sheets.append(sheet_name)

            if empty_sheets:
                console.print(f"[yellow]⚠️  {len(empty_sheets)} empty sheet(s) will be skipped:[/yellow]")
                for sheet in empty_sheets:
                    console.print(f"   [dim]• {sheet} (no data or header only)[/dim]")

            if non_empty_sheets:
                console.print(f"[green]✓ Found {len(non_empty_sheets)} sheet(s) with data[/green]")
            else:
                console.print(f"[yellow]⚠️  No sheets with data found[/yellow]")

            return non_empty_sheets if non_empty_sheets else None

        except Exception as e:
            error_str = str(e).lower()

            # Provide specific error messages for common issues
            if "404" in error_str or "not found" in error_str:
                console.print(f"\n[red]✗ Spreadsheet not found[/red]")
                console.print("\n[yellow]Possible causes:[/yellow]")
                console.print("  • Invalid spreadsheet ID or URL")
                console.print("  • Spreadsheet was deleted")
                console.print("  • You don't have access to this spreadsheet")
                console.print("\n[cyan]How to fix:[/cyan]")
                console.print("  1. Check the spreadsheet URL/ID is correct")
                console.print("  2. Make sure the spreadsheet is shared with your Google account")
                console.print(f"  3. Your account: check with [bold]dango auth list[/bold]")
                raise  # Re-raise to abort wizard
            elif "403" in error_str or "permission" in error_str or "forbidden" in error_str:
                console.print(f"\n[red]✗ Permission denied[/red]")
                console.print("\n[yellow]Possible causes:[/yellow]")
                console.print("  • You don't have access to this spreadsheet")
                console.print("  • Spreadsheet is not shared with your Google account")
                console.print("\n[cyan]How to fix:[/cyan]")
                console.print("  1. Share the spreadsheet with your Google account")
                console.print("  2. Or re-authenticate: [bold]dango auth google_sheets[/bold]")
                raise  # Re-raise to abort wizard
            elif "401" in error_str or "invalid" in error_str or "expired" in error_str or "refresh" in error_str:
                console.print(f"\n[red]✗ OAuth credential expired or invalid[/red]")
                console.print(f"[dim]Error details: {e}[/dim]")
                console.print("\n[cyan]How to fix:[/cyan]")
                console.print("  Re-authenticate: [bold]dango auth google_sheets[/bold]")
                raise  # Re-raise to abort wizard
            else:
                console.print(f"[yellow]Error fetching sheets: {e}[/yellow]")
                console.print(f"[dim]Error type: {type(e).__name__}[/dim]")
                raise  # Re-raise to abort wizard

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

        # Note: OAuth credentials are stored at sources.{source_type}.credentials.*
        # No oauth_ref needed - dlt finds credentials automatically

        # Add type-specific config block
        # Always create this block even if empty - it indicates the source type
        # and allows users to add config later
        # Convert source_type to config field name (e.g., "facebook_ads" -> "facebook_ads")
        config[source_type] = params if params else {}

        return config

    def _write_config_template(self, source_type: str, metadata: Dict[str, Any]) -> None:
        """
        Write default_config to .dlt/config.toml for pipeline stability.

        This writes the default configuration at source creation time so that:
        1. Users can customize the config before first sync
        2. Defaults don't change unexpectedly on Dango upgrades
        3. Config is visible and documented in user's project

        Args:
            source_type: Source type key (e.g., "google_analytics")
            metadata: Source metadata from registry containing default_config
        """
        try:
            import tomlkit

            default_config = metadata.get("default_config", {})
            if not default_config:
                return

            dlt_dir = self.project_root / ".dlt"
            config_path = dlt_dir / "config.toml"

            # Ensure .dlt directory exists
            dlt_dir.mkdir(parents=True, exist_ok=True)

            # Load existing config or create new
            if config_path.exists():
                doc = tomlkit.parse(config_path.read_text())
            else:
                doc = tomlkit.document()

            # Ensure [sources] table exists
            if "sources" not in doc:
                doc.add("sources", tomlkit.table())

            # Ensure [sources.{source_type}] table exists
            if source_type not in doc["sources"]:
                doc["sources"].add(source_type, tomlkit.table())

            # Write default_config values
            source_table = doc["sources"][source_type]

            for key, value in default_config.items():
                if key not in source_table:
                    # Add comment explaining this is a default that can be customized
                    if key == "queries":
                        # Special handling for queries (GA4)
                        source_table.add(tomlkit.comment(""))
                        source_table.add(tomlkit.comment("Default queries for Google Analytics 4"))
                        source_table.add(tomlkit.comment("Each query creates a table with the specified dimensions and metrics"))
                        source_table.add(tomlkit.comment("Customize by editing, adding, or removing queries"))
                        source_table.add(tomlkit.comment("GA4 API limits: max 9 dimensions, 10 metrics per query"))
                        source_table.add(tomlkit.comment("Docs: https://developers.google.com/analytics/devguides/reporting/data/v1"))
                        source_table.add(tomlkit.comment(""))

                    # Convert value to TOML-compatible format
                    source_table.add(key, value)

            # Write config file
            config_path.write_text(tomlkit.dumps(doc))
            console.print(f"[green]✅ Created config template: .dlt/config.toml[/green]")
            console.print(f"[dim]   Edit this file to customize {metadata.get('display_name')} queries[/dim]")

        except ImportError:
            console.print(f"[yellow]⚠️  tomlkit not installed - skipping config template[/yellow]")
        except Exception as e:
            console.print(f"[yellow]⚠️  Could not write config template: {e}[/yellow]")

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
