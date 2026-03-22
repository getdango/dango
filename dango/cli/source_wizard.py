"""dango/cli/source_wizard.py

Metadata-driven wizard that works for all 27+ data sources. Uses SOURCE_REGISTRY for display names, categories, and parameters.
"""

from pathlib import Path
from typing import Any

import inquirer
from inquirer import themes
from rich.console import Console
from rich.prompt import Confirm

from dango.cli.env_helpers import (
    create_env_template,
    guide_env_setup,
)
from dango.config.helpers import load_config, save_config
from dango.config.models import DataSource
from dango.ingestion.sources.registry import (
    SOURCE_REGISTRY,
    AuthType,
    get_all_categories,
    get_source_metadata,
    get_sources_by_category,
)
from dango.oauth.router import (
    OAUTH_PROVIDER_MAP,
    run_oauth_for_source,
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
            console.print(
                "[dim]Press Ctrl+C at any time to abort (nothing saved until the end)[/dim]\n"
            )

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

                    # Special handling for dlt_native sources — guided template wizard
                    if source_type == "dlt_native":
                        result = self._setup_dlt_native_source()
                        return result

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
                    if source_type == "rest_api":
                        params = self._collect_rest_api_params(source_name)
                    else:
                        params = self._collect_parameters(source_type, metadata, source_name)
                    if params == "← Back":
                        # Go back to source name
                        state = "name"
                        continue
                    if params is None:
                        return False  # User cancelled

                    # Step 4b: Resource selection (if source has available_resources)
                    selected = self._select_resources(source_type, metadata)
                    if selected is not None:
                        params["resources"] = selected

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
            source_config = self._create_source_config(source_name, source_type, params, metadata)

            # Step 8: If secrets required, validate credentials FIRST (before saving)
            if self.secret_params:
                console.print("\n[bold]Setting up credentials...[/bold]")

                # Create .env template
                create_env_template(self.env_file, self.secret_params)
                console.print("[green]✅ Created .env template[/green]")

                # Guide user through credential setup with validation
                # Pass setup_guide for detailed instructions
                setup_guide = metadata.get("setup_guide", [])
                validated = guide_env_setup(
                    self.env_file, self.secret_params, source_name, setup_guide
                )

                if not validated:
                    # Credentials not validated - don't save source config
                    console.print(
                        "\n[yellow]⚠️  Setup cancelled - credentials not validated[/yellow]"
                    )
                    console.print("\n[cyan]To retry:[/cyan]")
                    console.print("  dango source add")
                    return False

            # Step 9: Only save source config if validation passed or no secrets required
            self._save_source(source_config)
            console.print(f"\n[green]✅ Saved '{source_name}' to sources.yml[/green]")

            # Step 9b: Show setup guide + secrets.toml template for
            # sources with credentials in secrets.toml (not .env)
            if not self.secret_params and metadata.get("setup_guide"):
                console.print("\n[bold]Credential setup required:[/bold]")
                for line in metadata["setup_guide"]:
                    console.print(f"  {line}")

                secrets_template = metadata.get("secrets_toml_template")
                if secrets_template:
                    dlt_dir = self.project_root / ".dlt"
                    secrets_path = dlt_dir / "secrets.toml"
                    dlt_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        existing = secrets_path.read_text() if secrets_path.exists() else ""
                        section_header = f"[sources.{source_name}."
                        if section_header not in existing:
                            # Pre-populate template with wizard-collected params
                            # (e.g., zendesk subdomain). defaultdict(str) returns ""
                            # for unknown placeholders — prevents wizard crash from
                            # registry template typos (caught during manual testing).
                            from collections import defaultdict

                            template_vars = defaultdict(str, source_name=source_name, **params)
                            template_text = secrets_template.format_map(template_vars)
                            prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
                            new_content = prefix + template_text + "\n"
                            secrets_path.write_text(new_content)
                            console.print(
                                "\n[green]✓[/green] Added credential template to "
                                "[cyan].dlt/secrets.toml[/cyan]"
                            )
                            console.print(
                                "[yellow]Fill in the credential values before syncing.[/yellow]"
                            )
                        else:
                            console.print(
                                "\n[dim]Credential template already exists in "
                                ".dlt/secrets.toml[/dim]"
                            )
                    except Exception as e:
                        console.print(f"[yellow]Could not write secrets template: {e}[/yellow]")

            # Step 10: Offer automatic analysis metrics
            try:
                from dango.analysis.templates import generate_metrics_for_source

                templates = generate_metrics_for_source(source_type, source_name)
                if templates:
                    if Confirm.ask(
                        "\n[bold]Enable automatic analysis?[/bold]",
                        default=True,
                    ):
                        from dango.analysis.config import add_metrics_to_config

                        header = None
                        if source_type == "csv":
                            header = (
                                f"NOTE: Tables will be created in the raw_{source_name}"
                                f" schema. Replace 'your_table' with your actual"
                                f" table name after first sync."
                            )
                        add_metrics_to_config(self.project_root, templates, header_comment=header)
                        console.print(
                            f"[green]✅ Added {len(templates)} analysis"
                            f" metric(s) to metrics.yml[/green]"
                        )
            except Exception:
                pass  # Never block source add

            # Show first sync note if present (e.g., "large accounts may take 30 min")
            first_sync_note = metadata.get("first_sync_note")
            if first_sync_note:
                console.print(f"\n  [yellow]Note: {first_sync_note}[/yellow]")
                console.print(
                    "  [dim]You can press Ctrl+C during sync — progress is saved "
                    "and resumes on next run.[/dim]"
                )

            # Show lookback window setting if configured
            lookback = (metadata.get("default_config") or {}).get("lookback_days")
            if lookback:
                console.print(
                    f"\n  [dim]Lookback window: {lookback} day(s) — each incremental "
                    f"sync re-loads recent data to catch late-arriving records.[/dim]"
                )

            # Success messages based on whether secrets were required
            if self.secret_params:
                console.print(f"\n[green]✅ Source '{source_name}' fully configured![/green]")
                console.print("\n[cyan]Next steps:[/cyan]")
                console.print(f"  1. Sync your data:   dango sync --source {source_name}")
                console.print("  2. Schedule syncs:   dango schedule add")
            else:
                # No secrets required
                console.print(f"\n[green]✅ Source '{source_name}' added successfully![/green]")

                # Auto-validate configuration
                console.print("\n[dim]Validating configuration...[/dim]")
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
                    console.print("\n[bold cyan]What to do now:[/bold cyan]")
                    console.print("\n[bold]Option A: Use Web UI (recommended)[/bold]")
                    console.print("  1. Start platform: [cyan]dango start[/cyan]")
                    console.print("  2. Upload files via Web UI (sync happens automatically)")
                    console.print(
                        "  3. [dim](Optional)[/dim] Document tables: [cyan]dango docs[/cyan]"
                    )
                    console.print("\n[bold]Option B: Copy files manually[/bold]")
                    console.print(f"  1. Copy CSV files to: [cyan]{params['directory']}[/cyan]")
                    console.print(f"  2. Load data: [cyan]dango sync --source {source_name}[/cyan]")
                    console.print(
                        f"     • Creates dbt staging models in dbt/models/staging/{source_name}/"
                    )
                    console.print("     • Creates documentation file: sources.yml")
                    console.print(
                        "  3. [dim](Optional)[/dim] Document tables: [cyan]dango docs[/cyan]"
                    )
                    console.print("\n[dim]Notes:[/dim]")
                    console.print("  • All files must have same columns (first row = headers)")
                    console.print("  • Change folder/filters → .dango/sources.yml")
                    console.print(
                        f"  • Add column descriptions → dbt/models/staging/{source_name}/sources.yml"
                    )
                else:
                    console.print("\n[bold cyan]What to do now:[/bold cyan]")
                    console.print(
                        f"  1. Load your data: [cyan]dango sync --source {source_name}[/cyan]"
                    )
                    console.print(
                        f"     • This creates dbt staging models in dbt/models/staging/{source_name}/"
                    )
                    console.print(
                        f"     • Documentation file created: dbt/models/staging/{source_name}/sources.yml"
                    )
                    console.print(
                        "  2. Document your tables (optional): Edit sources.yml to add descriptions"
                    )
                    console.print("     • Regenerate docs: [cyan]dango docs[/cyan]")
                    console.print("\n[dim]To customize later:[/dim]")
                    console.print("  • Change connection settings → .dango/sources.yml")
                    console.print(
                        f"  • Update column descriptions → dbt/models/staging/{source_name}/sources.yml (created after first sync)"
                    )

            return True

        except KeyboardInterrupt:
            console.print("\n\n[yellow]Wizard cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]❌ Error: {e}[/red]")
            return False

    def _select_source_flat(self) -> str | None:
        """Select source from flat list (no categories)"""
        # Get all wizard-enabled sources
        all_sources = []
        for source_type, source_meta in SOURCE_REGISTRY.items():
            if source_meta.get("wizard_enabled", False):
                display_name = source_meta.get("display_name", source_type)
                all_sources.append((display_name, source_type))

        if not all_sources:
            console.print("[yellow]No sources available[/yellow]")
            return None

        # Sort alphabetically by display name
        all_sources.sort(key=lambda x: x[0].lower())

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

    def _select_category(self) -> str | None:
        """Select source category (deprecated - kept for reference)"""
        categories = get_all_categories()

        # Create display with counts and examples
        choices = []
        for category in categories:
            sources_in_category = get_sources_by_category(category)
            # Filter to only wizard-enabled sources
            available = [
                s
                for s in sources_in_category
                if s in SOURCE_REGISTRY and SOURCE_REGISTRY[s].get("wizard_enabled", False)
            ]
            count = len(available)

            # Skip categories with no wizard-enabled sources
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

    def _select_source(self, category: str) -> str | None:
        """Select specific source from category"""
        sources = get_sources_by_category(category)

        # Filter to only wizard-enabled sources
        available_sources = [
            s
            for s in sources
            if s in SOURCE_REGISTRY and SOURCE_REGISTRY[s].get("wizard_enabled", False)
        ]

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

    def _select_resources(self, source_type: str, metadata: dict[str, Any]) -> list[str] | None:
        """Prompt user to select which resources to sync.

        Only shown for sources with ``available_resources`` in registry.
        Sources that already have a ``resources`` multiselect param in
        optional_params (e.g., HubSpot) are skipped — their resources
        are collected via the normal parameter flow.

        Returns:
            Selected resource list, or None if not applicable.
        """
        available = metadata.get("available_resources")
        if not available:
            return None

        # Skip if resources are already collected via optional_params
        for p in metadata.get("optional_params", []):
            if p.get("name") == "resources":
                return None

        defaults = metadata.get("default_resources", available)

        console.print("\n[bold]Select resources to sync:[/bold]")
        console.print("[dim](Space to toggle, Enter to confirm)[/dim]")

        questions = [
            inquirer.Checkbox(
                "resources",
                message="Resources",
                choices=available,
                default=defaults,
            ),
        ]
        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return defaults  # Ctrl+C during selection — use defaults
        return answers.get("resources", defaults) or defaults

    def _show_source_info(self, metadata: dict[str, Any]) -> None:
        """Display source information"""
        console.print(f"\n[bold]{metadata.get('display_name')}[/bold]")
        console.print(f"{metadata.get('description')}\n")

        if metadata.get("cost_warning"):
            console.print(f"[yellow]💰 {metadata['cost_warning']}[/yellow]\n")

        # Skip setup_guide - instructions shown at end after config

        if metadata.get("docs_url"):
            console.print(f"[dim]📚 Docs: {metadata['docs_url']}[/dim]\n")

    def _handle_oauth_setup(
        self, source_type: str, source_name: str, metadata: dict[str, Any]
    ) -> str | None:
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
            console.print(
                f"[yellow]⚠️  OAuth required but provider not yet implemented for {source_type}[/yellow]"
            )
            console.print(
                "[yellow]   You'll need to configure credentials manually in .dlt/secrets.toml[/yellow]\n"
            )
            return None

        # Check for existing OAuth credentials for this source type
        oauth_storage = OAuthStorage(self.project_root)
        existing_cred = oauth_storage.get(source_type)

        if existing_cred:
            # Credentials exist for this source type
            if existing_cred.is_expired():
                console.print(f"[red]⚠️  OAuth credentials for {source_type} have expired[/red]")
                console.print(f"[yellow]Re-authenticate with: dango oauth {source_type}[/yellow]\n")

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
                console.print(
                    f"[green]✓ OAuth credentials found: {existing_cred.account_info}[/green]\n"
                )
                return None

        # No existing credentials - prompt to set up new OAuth
        console.print("[yellow]⚠️  OAuth authentication required[/yellow]")
        console.print("[cyan]This source requires OAuth credentials to access your data.[/cyan]\n")

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
            console.print("\n[yellow]⚠️  Skipping OAuth setup[/yellow]")
            console.print("[cyan]To authenticate later, run:[/cyan]")
            console.print(f"  dango oauth {source_type}")
            console.print(
                "\n[dim]You can still configure this source, but you won't be able to sync"
            )
            console.print("until you set up OAuth credentials.[/dim]\n")
            return None

        # "Set up OAuth now" - run OAuth flow
        console.print(
            f"\n[bold]Starting OAuth setup for {metadata.get('display_name')}...[/bold]\n"
        )

        success = run_oauth_for_source(source_type, source_name, self.project_root)

        if success:
            console.print("\n[green]✅ OAuth credentials configured successfully![/green]")
            console.print("[dim]  Credentials saved to .dlt/secrets.toml[/dim]\n")
            return None
        else:
            console.print("\n[red]❌ OAuth setup failed[/red]")
            console.print(
                f"[yellow]You can try again later with: dango oauth {source_type}[/yellow]\n"
            )

            continue_anyway = Confirm.ask(
                "Continue configuring source without OAuth credentials?", default=False
            )

            if continue_anyway:
                console.print("[yellow]⚠️  Continuing without credentials[/yellow]")
                console.print(
                    "[yellow]   You won't be able to sync until you authenticate[/yellow]\n"
                )
                return None
            else:
                return "back"

    def _get_source_name(self, source_type_key: str, metadata: dict[str, Any]) -> str | None:
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
            console.print(
                "[cyan]Use lowercase with underscores (e.g., 'my_sales_data', 'prod_analytics')[/cyan]"
            )
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
                console.print(
                    "[yellow]⚠️  Invalid format. Use letters, numbers, and underscores only (no hyphens).[/yellow]"
                )
                continue

            # Use name as-is (no auto-prefixing)
            final_source_name = user_input

            # Check if final name already exists
            if self._source_name_exists(final_source_name):
                console.print(
                    f"[yellow]⚠️  Source '{final_source_name}' already exists. Choose a different name.[/yellow]"
                )
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

    def _is_credential_param(self, param: dict[str, Any], source_type: str) -> bool:
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
            "credentials",
            "credential",
            "access_token",
            "api_key",
            "secret",
            "_env",  # Parameters ending in _env are typically env var references
        ]

        for pattern in credential_patterns:
            if pattern in param_name:
                return True

        # Source-specific credential parameters that are collected during OAuth
        # These are stored in .dlt/secrets.toml by the OAuth provider
        oauth_collected_params = {
            "facebook_ads": [],  # account_id is a required wizard param, not OAuth-collected
            "google_ads": ["customer_id"],  # Google Ads OAuth collects customer_id
            "shopify": ["shop_url"],  # Shopify OAuth collects shop_url
        }

        if source_type in oauth_collected_params:
            if param_name in oauth_collected_params[source_type]:
                return True

        return False

    def _setup_dlt_native_source(self) -> bool:
        """Guided setup for dlt_native sources — generates template + registers in sources.yml.

        Creates a Python source template in custom_sources/ and registers the
        source in sources.yml. Bypasses the normal state machine since it handles
        both file creation and config save internally.

        Returns:
            True if source created successfully, False otherwise.
        """
        console.print("\n[bold]Custom dlt Source Setup[/bold]")
        console.print("[dim]This creates a Python template and registers it in sources.yml[/dim]\n")

        # 1. Module name
        questions = [
            inquirer.Text(
                "module_name",
                message="Python module name (e.g., my_api)",
            )
        ]
        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return False
        module_name = answers["module_name"].strip()
        if not module_name.isidentifier():
            console.print(
                f"[red]'{module_name}' is not a valid Python identifier. "
                f"Use letters, numbers, and underscores (cannot start with a number).[/red]"
            )
            return False

        # 2. Function name
        default_func = f"{module_name}_source"
        questions = [
            inquirer.Text(
                "function_name",
                message="Source function name",
                default=default_func,
            )
        ]
        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return False
        function_name = answers["function_name"].strip()
        if not function_name.isidentifier():
            console.print(f"[red]'{function_name}' is not a valid Python identifier.[/red]")
            return False

        # 3. Source name for sources.yml
        default_source_name = module_name
        questions = [
            inquirer.Text(
                "source_name",
                message="Source name (used in sources.yml and sync commands)",
                default=default_source_name,
            )
        ]
        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return False
        source_name = answers["source_name"].strip().lower()
        if not source_name.replace("_", "").isalnum():
            console.print(
                f"[red]'{source_name}' is invalid. "
                f"Use only lowercase letters, numbers, and underscores.[/red]"
            )
            return False

        # 4. Check for duplicate source names (before writing any files)
        config = load_config(self.project_root)
        existing_names = {s.name for s in config.sources.sources}
        if source_name in existing_names:
            console.print(
                f"[red]A source named '{source_name}' already exists in sources.yml.[/red]"
            )
            return False

        # 5. Check if template file already exists
        custom_dir = self.project_root / "custom_sources"
        template_path = custom_dir / f"{module_name}.py"
        if template_path.exists():
            if not Confirm.ask(
                f"[yellow]{template_path.relative_to(self.project_root)} already exists. Overwrite?[/yellow]",
                default=False,
            ):
                console.print("[dim]Aborted — existing file preserved.[/dim]")
                return False

        # 6. Generate template file
        custom_dir.mkdir(parents=True, exist_ok=True)
        template_content = f'''"""custom_sources/{module_name}.py

Custom dlt source for {source_name}.
Generated by dango source wizard.

Documentation: https://dlthub.com/docs/general-usage/source
"""

import dlt


@dlt.source
def {function_name}(api_key: str = dlt.secrets.value):
    """Load data from your API.

    Args:
        api_key: API key (resolved from .dlt/secrets.toml or environment).
    """
    yield {module_name}_resource(api_key)


@dlt.resource(write_disposition="replace")
def {module_name}_resource(api_key: str):
    """Fetch data from the API.

    Modify this function to call your API and yield records (dicts).
    """
    # TODO: Replace with your API calls
    # Example:
    #   import requests
    #   response = requests.get(
    #       "https://api.example.com/data",
    #       headers={{"Authorization": f"Bearer {{api_key}}"}},
    #   )
    #   yield from response.json()["items"]
    yield {{"id": 1, "name": "example"}}
'''
        template_path.write_text(template_content)
        console.print(
            f"[green]✅ Created template: {template_path.relative_to(self.project_root)}[/green]"
        )

        # 7. Register in sources.yml
        source_config: dict[str, Any] = {
            "name": source_name,
            "type": "dlt_native",
            "enabled": True,
            "description": f"Custom dlt source - {module_name}",
            "dlt_native": {
                "source_module": module_name,
                "source_function": function_name,
            },
        }
        self._save_source(source_config)
        console.print(f"[green]✅ Registered '{source_name}' in sources.yml[/green]")

        # 8. Next steps
        console.print("\n[bold cyan]Next steps:[/bold cyan]")
        console.print(
            f"  1. Edit [cyan]{template_path.relative_to(self.project_root)}[/cyan] "
            f"— add your API calls"
        )
        console.print("  2. Add credentials to [cyan].dlt/secrets.toml[/cyan] or [cyan].env[/cyan]")
        console.print(f"  3. Test: [cyan]dango sync --source {source_name}[/cyan]")
        console.print("\n[dim]dlt docs: https://dlthub.com/docs/general-usage/source[/dim]")
        return True

    def _collect_rest_api_params(self, source_name: str) -> dict[str, Any] | str | None:
        """Guided REST API parameter collection with per-auth-type prompts.

        Replaces generic _collect_parameters() for rest_api sources with a
        step-by-step flow: base URL, auth type, auth credentials, endpoints.

        Args:
            source_name: User-chosen source instance name

        Returns:
            Parameter dict on success, "← Back" to go back, None on cancel.
        """
        console.print("[bold]REST API Configuration[/bold]")
        console.print("[dim]Type 'back' for Base URL to return to source name[/dim]\n")

        # 1. Base URL
        questions = [
            inquirer.Text(
                "base_url",
                message="Base URL (e.g., https://api.example.com)",
            )
        ]
        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return None
        if answers["base_url"].strip().lower() == "back":
            return "← Back"
        base_url = answers["base_url"].strip()

        # 2. Auth type
        auth_choices = [
            ("Bearer Token", "bearer"),
            ("API Key (header or query param)", "api_key"),
            ("HTTP Basic (username + password)", "basic"),
            ("OAuth2 Client Credentials", "oauth2_client_credentials"),
            ("No Authentication", "none"),
        ]
        questions = [
            inquirer.List(
                "auth_type",
                message="Authentication method",
                choices=auth_choices,
            )
        ]
        answers = inquirer.prompt(questions, theme=themes.GreenPassion())
        if not answers:
            return None
        auth_type = answers["auth_type"]

        params: dict[str, Any] = {
            "base_url": base_url,
            "auth_type": auth_type,
        }

        # 3. Auth-type-specific credential prompts
        env_prefix = source_name.upper().replace("-", "_")
        if auth_type == "bearer":
            default_env = f"{env_prefix}_API_TOKEN"
            questions = [
                inquirer.Text(
                    "auth_token_env",
                    message="Environment variable for bearer token",
                    default=default_env,
                )
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None
            params["auth_token_env"] = answers["auth_token_env"].strip()
            self.secret_params.append(
                {"name": params["auth_token_env"], "description": f"Bearer token for {source_name}"}
            )

        elif auth_type == "api_key":
            default_env = f"{env_prefix}_API_KEY"
            questions = [
                inquirer.Text(
                    "auth_token_env",
                    message="Environment variable for API key value",
                    default=default_env,
                ),
                inquirer.Text(
                    "api_key_name",
                    message="Header/query parameter name (e.g., X-API-Key)",
                    default="X-API-Key",
                ),
                inquirer.List(
                    "api_key_location",
                    message="Send API key in",
                    choices=[("Header", "header"), ("Query parameter", "query")],
                ),
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None
            params["auth_token_env"] = answers["auth_token_env"].strip()
            params["api_key_name"] = answers["api_key_name"].strip()
            params["api_key_location"] = answers["api_key_location"]
            self.secret_params.append(
                {"name": params["auth_token_env"], "description": f"API key for {source_name}"}
            )

        elif auth_type == "basic":
            default_user_env = f"{env_prefix}_USERNAME"
            default_pass_env = f"{env_prefix}_PASSWORD"
            questions = [
                inquirer.Text(
                    "basic_username_env",
                    message="Environment variable for username",
                    default=default_user_env,
                ),
                inquirer.Text(
                    "basic_password_env",
                    message="Environment variable for password",
                    default=default_pass_env,
                ),
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None
            params["basic_username_env"] = answers["basic_username_env"].strip()
            params["basic_password_env"] = answers["basic_password_env"].strip()
            self.secret_params.extend(
                [
                    {
                        "name": params["basic_username_env"],
                        "description": f"Username for {source_name}",
                    },
                    {
                        "name": params["basic_password_env"],
                        "description": f"Password for {source_name}",
                    },
                ]
            )

        elif auth_type == "oauth2_client_credentials":
            default_id_env = f"{env_prefix}_CLIENT_ID"
            default_secret_env = f"{env_prefix}_CLIENT_SECRET"
            questions = [
                inquirer.Text(
                    "access_token_url",
                    message="Token endpoint URL (e.g., https://auth.example.com/oauth/token)",
                ),
                inquirer.Text(
                    "client_id_env",
                    message="Environment variable for client ID",
                    default=default_id_env,
                ),
                inquirer.Text(
                    "client_secret_env",
                    message="Environment variable for client secret",
                    default=default_secret_env,
                ),
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None
            params["access_token_url"] = answers["access_token_url"].strip()
            params["client_id_env"] = answers["client_id_env"].strip()
            params["client_secret_env"] = answers["client_secret_env"].strip()
            self.secret_params.extend(
                [
                    {
                        "name": params["client_id_env"],
                        "description": f"OAuth2 client ID for {source_name}",
                    },
                    {
                        "name": params["client_secret_env"],
                        "description": f"OAuth2 client secret for {source_name}",
                    },
                ]
            )

        # 4. Endpoint collection
        console.print("\n[bold]API Endpoints[/bold]")
        console.print("[dim]Add one or more endpoints to sync[/dim]")
        endpoints: list[dict[str, str]] = []
        while True:
            questions = [
                inquirer.Text(
                    "path",
                    message="Endpoint path (e.g., /users, /orders)",
                ),
                inquirer.Text(
                    "name",
                    message="Resource name (table name in DuckDB)",
                ),
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers:
                return None
            path = answers["path"].strip()
            name = answers["name"].strip() or path.strip("/").replace("/", "_")
            endpoints.append({"path": path, "name": name})
            console.print(f"  [green]✓[/green] Added: {path} → {name}")

            if not Confirm.ask("Add another endpoint?", default=False):
                break

        params["endpoints"] = endpoints
        return params

    def _collect_parameters(
        self, source_type_key: str, metadata: dict[str, Any], source_name: str
    ) -> dict[str, Any] | None:
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
                    console.print(
                        f"  [green]✓ {param.get('prompt', param['name'])}: Using OAuth credentials[/green]"
                    )
                    continue

                # Inject source_name into directory default for CSV sources
                if param["name"] == "directory" and param.get("default") == "data/uploads":
                    param = param.copy()  # Don't modify registry
                    param["default"] = f"data/uploads/{source_name}"

                value = self._prompt_parameter(
                    param, source_name, source_type_display, metadata, required=True
                )
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
            console.print(
                "\n[bold]Optional settings[/bold] [dim](press Enter to use defaults, edit .dango/sources.yml to change later)[/dim]"
            )
            for param in optional_params:
                # Skip auth credential params when user selected "none" auth
                if param["name"] == "auth_token_env" and params.get("auth_type") == "none":
                    continue
                value = self._prompt_parameter(
                    param, source_name, source_type_display, metadata, required=False
                )
                # Check if user wants to go back
                if isinstance(value, str) and value.lower() == "back":
                    return "← Back"
                if value is not None:
                    params[param["name"]] = value

        return params

    def _prompt_parameter(
        self,
        param: dict[str, Any],
        source_name: str,
        source_type: str,
        metadata: dict[str, Any],
        required: bool = True,
    ) -> Any | None:
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

            # Generate unique env var by replacing service prefix with source name
            # Examples:
            #   slack + SLACK_ACCESS_TOKEN → SLACK_ACCESS_TOKEN
            #   marketing_slack + SLACK_ACCESS_TOKEN → MARKETING_SLACK_ACCESS_TOKEN
            #   stripe_test + STRIPE_API_KEY → STRIPE_TEST_API_KEY

            # Extract suffix from base env var (everything after first _)
            # STRIPE_API_KEY → API_KEY, SHOPIFY_ACCESS_TOKEN → ACCESS_TOKEN
            if "_" in base_env_var:
                suffix = "_".join(base_env_var.split("_")[1:])
                source_prefix = name_suffix.upper().replace("-", "_")
                env_var = f"{source_prefix}_{suffix}"
            else:
                # Fallback: just append name suffix
                env_var = f"{base_env_var}_{name_suffix.upper().replace('-', '_')}"

            # Check if env var already exists in .env
            env_exists = False
            if self.env_file.exists():
                env_content = self.env_file.read_text()
                for line in env_content.split("\n"):
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if "=" in line:
                            key, value = line.split("=", 1)
                            if key.strip() == env_var and value.strip():
                                env_exists = True
                                break

            # Store secret metadata for .env template creation (only if not already set)
            if not env_exists:
                secret_metadata = {
                    "name": env_var,
                    "display_name": param.get("prompt", env_var),
                    "help": help_text or param.get("help", ""),
                    "format": param.get("format", ""),
                    "example": param.get("example", ""),
                    "source_name": source_name,  # Track which source this key is for
                    "source_type": source_type,
                }
                self.secret_params.append(secret_metadata)
                console.print(f"  [cyan]→ Credential for '{source_name}' will be: {env_var}[/cyan]")
                console.print("    [dim]You'll set this value in .env file[/dim]")
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
            except Exception:
                # OAuth authentication failed - abort wizard
                console.print("\n[red]Cannot continue without valid OAuth credentials.[/red]")
                console.print("[yellow]Please re-authenticate first, then try again.[/yellow]")
                return None

            if sheets is None:
                # No OAuth configured yet - fall back to manual entry
                console.print(
                    "[yellow]⚠️  No OAuth configured. Enter sheet names manually.[/yellow]"
                )
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
                console.print("[yellow]⚠️  No sheets found in spreadsheet[/yellow]")
                return None

            # Loop until user confirms selection
            while True:
                # Show clear instructions before the checkbox
                console.print("\n[bold]Select sheets to load:[/bold]")
                console.print(
                    "[cyan]  ↑/↓  Navigate    Space  Select/deselect    Enter  Confirm[/cyan]\n"
                )

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
                    console.print("[yellow]⚠️  You must select at least one sheet[/yellow]")
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

        elif param_type == "list":
            # Comma-separated list input (e.g., table_names, collection_names)
            questions = [
                inquirer.Text(
                    param_name,
                    message=prompt + (" (optional)" if not required else ""),
                    default=None,
                )
            ]

        elif param_type == "json":
            # JSON input (e.g., REST API config)
            questions = [
                inquirer.Text(
                    param_name,
                    message=prompt + (" (optional)" if not required else ""),
                    default=str(default) if default is not None else None,
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

        # Parse comma-separated input into list for list-type params
        # (after skip/empty checks so empty input returns None, not [])
        if param_type == "list" and value and isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()] or None

        # Parse JSON string into Python object for json-type params
        if param_type == "json" and value and isinstance(value, str):
            import json

            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                console.print(f"[red]Invalid JSON: {value}[/red]")
                return None

        # Cast integer/number type params
        if param_type == "integer" and value and isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                console.print(f"[red]Invalid integer: {value}[/red]")
                return None
        elif param_type == "number" and value and isinstance(value, str):
            try:
                num = float(value)
                value = int(num) if num.is_integer() else num
            except ValueError:
                console.print(f"[red]Invalid number: {value}[/red]")
                return None

        # Show incremental loading education for start_date parameters
        if param_name == "start_date" and value:
            console.print("\n[cyan]ℹ️  About Incremental Loading:[/cyan]")
            console.print("  • start_date is only used for the FIRST sync")
            console.print("  • Future syncs load NEW data since last run")
            console.print("  • Cursor tracks when record was CREATED, not event date")
            console.print("  • Example: Dec 31 order might have created=Jan 1")
            console.print(
                "\n[yellow]💡 Tip: Set start_date 7-14 days earlier to catch late data[/yellow]\n"
            )

        return value

    def _fetch_google_sheets(self, source_name: str) -> list[str] | None:
        """
        Fetch sheet/tab names from a Google Spreadsheet using OAuth credentials.

        Args:
            source_name: Source name (used to find OAuth credentials)

        Returns:
            List of sheet names, or None if failed
        """
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            # Get OAuth credentials for Google Sheets
            oauth_storage = OAuthStorage(self.project_root)
            cred = oauth_storage.get("google_sheets")

            if not cred:
                console.print("[yellow]No Google Sheets OAuth credentials found[/yellow]")
                console.print("[dim]Run 'dango oauth google_sheets' first[/dim]")
                return None

            # Get credentials from the OAuthCredential object
            tokens = cred.credentials
            if not tokens:
                console.print("[yellow]Could not get OAuth tokens[/yellow]")
                return None

            # Get scopes from metadata (saved during OAuth authentication)
            scopes = cred.metadata.get("scopes", []) if cred.metadata else []

            # Debug: Check what we have
            if not tokens.get("refresh_token"):
                console.print("[red]Error: No refresh token found in credentials[/red]")
                console.print("[dim]This usually means OAuth wasn't completed properly[/dim]")
                console.print("[cyan]Run: dango oauth google_sheets[/cyan]")
                return None

            if not tokens.get("client_id") or not tokens.get("client_secret"):
                console.print("[red]Error: Missing client_id or client_secret[/red]")
                console.print("[dim]OAuth configuration is incomplete[/dim]")
                console.print("[cyan]Run: dango oauth google_sheets[/cyan]")
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
                console.print("[red]Failed to refresh OAuth token[/red]")
                console.print(f"[dim]Details: {refresh_error}[/dim]")
                console.print(f"[dim]Error type: {type(refresh_error).__name__}[/dim]")
                # Re-raise so the outer try-except can handle it
                raise

            # Build Sheets API service
            service = build("sheets", "v4", credentials=credentials, cache_discovery=False)

            # Get spreadsheet ID from the collected params
            # This is a bit tricky since we're in the middle of collecting params
            # We need to access the params collected so far
            # The spreadsheet_url_or_id should already be collected before range_names
            spreadsheet_id = getattr(self, "_current_spreadsheet_id", None)

            if not spreadsheet_id:
                console.print("[yellow]Spreadsheet ID not yet collected[/yellow]")
                return None

            # Extract ID from URL if needed
            if "docs.google.com" in spreadsheet_id:
                # URL format: https://docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit
                import re

                match = re.search(r"/d/([a-zA-Z0-9-_]+)", spreadsheet_id)
                if match:
                    spreadsheet_id = match.group(1)

            # Fetch spreadsheet metadata
            console.print("[dim]Fetching sheets from spreadsheet...[/dim]")
            result = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

            # Extract sheet names
            sheets = result.get("sheets", [])
            sheet_names = [sheet["properties"]["title"] for sheet in sheets]

            # Check each sheet for data (need at least 2 rows: header + 1 data row)
            # Fetch first 2 rows of each sheet to determine if empty
            console.print("[dim]Checking for empty sheets...[/dim]")
            non_empty_sheets = []
            empty_sheets = []

            for sheet_name in sheet_names:
                try:
                    # Fetch just the first 2 rows to check if sheet has data
                    range_check = f"'{sheet_name}'!A1:Z2"
                    data_result = (
                        service.spreadsheets()
                        .values()
                        .get(spreadsheetId=spreadsheet_id, range=range_check)
                        .execute()
                    )

                    values = data_result.get("values", [])
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
                console.print(
                    f"[yellow]⚠️  {len(empty_sheets)} empty sheet(s) will be skipped:[/yellow]"
                )
                for sheet in empty_sheets:
                    console.print(f"   [dim]• {sheet} (no data or header only)[/dim]")

            if non_empty_sheets:
                console.print(f"[green]✓ Found {len(non_empty_sheets)} sheet(s) with data[/green]")
            else:
                console.print("[yellow]⚠️  No sheets with data found[/yellow]")

            return non_empty_sheets if non_empty_sheets else None

        except Exception as e:
            error_str = str(e).lower()

            # Provide specific error messages for common issues
            if "404" in error_str or "not found" in error_str:
                console.print("\n[red]✗ Spreadsheet not found[/red]")
                console.print("\n[yellow]Possible causes:[/yellow]")
                console.print("  • Invalid spreadsheet ID or URL")
                console.print("  • Spreadsheet was deleted")
                console.print("  • You don't have access to this spreadsheet")
                console.print("\n[cyan]How to fix:[/cyan]")
                console.print("  1. Check the spreadsheet URL/ID is correct")
                console.print("  2. Make sure the spreadsheet is shared with your Google account")
                console.print("  3. Your account: check with [bold]dango oauth list[/bold]")
                raise  # Re-raise to abort wizard
            elif "403" in error_str or "permission" in error_str or "forbidden" in error_str:
                console.print("\n[red]✗ Permission denied[/red]")
                console.print("\n[yellow]Possible causes:[/yellow]")
                console.print("  • You don't have access to this spreadsheet")
                console.print("  • Spreadsheet is not shared with your Google account")
                console.print("\n[cyan]How to fix:[/cyan]")
                console.print("  1. Share the spreadsheet with your Google account")
                console.print("  2. Or re-authenticate: [bold]dango oauth google_sheets[/bold]")
                raise  # Re-raise to abort wizard
            elif (
                "401" in error_str
                or "invalid" in error_str
                or "expired" in error_str
                or "refresh" in error_str
            ):
                console.print("\n[red]✗ OAuth credential expired or invalid[/red]")
                console.print(f"[dim]Error details: {e}[/dim]")
                console.print("\n[cyan]How to fix:[/cyan]")
                console.print("  Re-authenticate: [bold]dango oauth google_sheets[/bold]")
                raise  # Re-raise to abort wizard
            else:
                console.print(f"[yellow]Error fetching sheets: {e}[/yellow]")
                console.print(f"[dim]Error type: {type(e).__name__}[/dim]")
                raise  # Re-raise to abort wizard

    def _create_source_config(
        self,
        source_name: str,
        source_type: str,
        params: dict[str, Any],
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
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
        # Sources with a dedicated DataSource field use that field name;
        # all others use generic_config (e.g., postgres, mongodb)
        from dango.config.models import DataSource

        if source_type in DataSource.model_fields:
            config[source_type] = params if params else {}
        else:
            config["generic_config"] = params if params else {}

        return config

    def _write_config_template(self, source_type: str, metadata: dict[str, Any]) -> None:
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
                        source_table.add(
                            tomlkit.comment(
                                "Each query creates a table with the specified dimensions and metrics"
                            )
                        )
                        source_table.add(
                            tomlkit.comment("Customize by editing, adding, or removing queries")
                        )
                        source_table.add(
                            tomlkit.comment(
                                "GA4 API limits: max 9 dimensions, 10 metrics per query"
                            )
                        )
                        source_table.add(
                            tomlkit.comment(
                                "Docs: https://developers.google.com/analytics/devguides/reporting/data/v1"
                            )
                        )
                        source_table.add(tomlkit.comment(""))

                    # Convert value to TOML-compatible format
                    source_table.add(key, value)

            # Write config file
            config_path.write_text(tomlkit.dumps(doc))
            console.print("[green]✅ Created config template: .dlt/config.toml[/green]")
            console.print(
                f"[dim]   Edit this file to customize {metadata.get('display_name')} queries[/dim]"
            )

        except ImportError:
            console.print("[yellow]⚠️  tomlkit not installed - skipping config template[/yellow]")
        except Exception as e:
            console.print(f"[yellow]⚠️  Could not write config template: {e}[/yellow]")

    def _save_source(self, source_config: dict[str, Any]) -> None:
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
