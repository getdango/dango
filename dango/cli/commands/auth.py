"""dango/cli/commands/auth.py

OAuth authentication commands.
"""

import click

from dango.cli import console


@click.group()
@click.pass_context
def auth(ctx: click.Context) -> None:
    """
    Authenticate with OAuth providers.

    Commands:
      dango auth google_sheets      Authenticate with Google Sheets
      dango auth google_analytics   Authenticate with Google Analytics (GA4)
      dango auth google_ads         Authenticate with Google Ads
      dango auth facebook_ads       Authenticate with Facebook Ads
    """
    pass


@auth.command("list")
@click.pass_context
def auth_list(ctx: click.Context) -> None:
    """
    List all OAuth credentials

    Shows all configured OAuth credentials with account info, expiry status, and usage.
    """
    from rich.table import Table

    from dango.oauth.storage import OAuthStorage

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Get all OAuth credentials
        credentials = oauth_storage.list()

        if not credentials:
            console.print("\n[yellow]No OAuth credentials configured[/yellow]")
            console.print("\n[cyan]To authenticate:[/cyan]")
            console.print("  dango auth google_sheets")
            console.print("  dango auth google_analytics")
            console.print("  dango auth google_ads")
            console.print("  dango auth facebook_ads")
            return

        # Create table
        table = Table(title=f"OAuth Credentials ({len(credentials)})", show_header=True)
        table.add_column("Source Type", style="cyan")
        table.add_column("Provider", style="blue")
        table.add_column("Account", style="green")
        table.add_column("Status", style="yellow")
        table.add_column("Created", style="dim")

        for cred in credentials:
            # Determine status
            if cred.is_expired():
                status = "[red]EXPIRED[/red]"
            elif cred.is_expiring_soon():
                days_left = cred.days_until_expiry()
                status = f"[yellow]Expires in {days_left}d[/yellow]"
            else:
                status = "[green]Active[/green]"

            # Format created date
            created = cred.created_at.strftime("%Y-%m-%d") if cred.created_at else "Unknown"

            table.add_row(cred.source_type, cred.provider, cred.account_info, status, created)

        console.print("\n")
        console.print(table)
        console.print("\n[dim]To re-authenticate: dango auth <source_type>[/dim]")
        console.print("[dim]To remove: dango auth remove <source_type>[/dim]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("status")
@click.pass_context
def auth_status(ctx: click.Context) -> None:
    """
    Show OAuth credential expiry status

    Displays OAuth credentials that are expired or expiring soon.
    """
    from dango.oauth.storage import OAuthStorage

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Get all OAuth credentials
        credentials = oauth_storage.list()

        if not credentials:
            console.print("\n[yellow]No OAuth credentials configured[/yellow]\n")
            return

        # Find credentials that need attention
        expired = [c for c in credentials if c.is_expired()]
        expiring_soon = [c for c in credentials if c.is_expiring_soon() and not c.is_expired()]

        if not expired and not expiring_soon:
            console.print("\n[green]✓ All OAuth credentials are active[/green]\n")
            return

        # Show expired credentials
        if expired:
            console.print("\n[red]⚠️  Expired OAuth Credentials:[/red]")
            for cred in expired:
                console.print(f"  • {cred.account_info} ({cred.source_type})")
                expiry_str = cred.expires_at.strftime("%Y-%m-%d") if cred.expires_at else "Unknown"
                console.print(f"    [dim]Expired: {expiry_str}[/dim]")
                console.print(
                    f"    [yellow]Re-authenticate: dango auth refresh {cred.source_type}[/yellow]\n"
                )

        # Show expiring soon
        if expiring_soon:
            console.print("\n[yellow]⚠️  OAuth Credentials Expiring Soon:[/yellow]")
            for cred in expiring_soon:
                days_left = cred.days_until_expiry()
                console.print(f"  • {cred.account_info} ({cred.source_type})")
                console.print(
                    f"    [dim]Expires: {cred.expires_at.strftime('%Y-%m-%d') if cred.expires_at else 'Unknown'} ({days_left} days)[/dim]"
                )
                console.print(
                    f"    [cyan]Re-authenticate: dango auth refresh {cred.source_type}[/cyan]\n"
                )

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("remove")
@click.argument("source_type")
@click.pass_context
def auth_remove(ctx: click.Context, source_type: str) -> None:
    """
    Remove OAuth credential

    SOURCE_TYPE: Source type to remove credentials for (e.g., google_ads, facebook_ads)

    Example:
      dango auth remove google_ads
    """
    from rich.prompt import Confirm

    from dango.oauth.storage import OAuthStorage

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Check if credential exists
        cred = oauth_storage.get(source_type)
        if not cred:
            console.print(f"\n[red]✗ OAuth credentials for '{source_type}' not found[/red]")
            console.print("\n[cyan]To see all credentials:[/cyan] dango auth list\n")
            raise click.Abort()

        # Show info and confirm
        console.print("\n[yellow]⚠️  About to remove OAuth credentials:[/yellow]")
        console.print(f"  Source Type: {cred.source_type}")
        console.print(f"  Provider: {cred.provider}")
        console.print(f"  Account: {cred.account_info}\n")

        if not Confirm.ask("[red]Are you sure?[/red]", default=False):
            console.print("\n[yellow]Cancelled[/yellow]\n")
            return

        # Remove credential
        if oauth_storage.delete(source_type):
            console.print("\n[green]✓ OAuth credential removed successfully[/green]\n")
        else:
            console.print("\n[red]✗ Failed to remove OAuth credential[/red]\n")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("refresh")
@click.argument("oauth_name")
@click.pass_context
def auth_refresh(ctx: click.Context, oauth_name: str) -> None:
    """
    Re-authenticate OAuth credential

    OAUTH_NAME: Name of OAuth credential to refresh (from dango auth list)

    Example:
      dango auth refresh facebook_ads_123456789
    """
    from dango.oauth import create_oauth_manager
    from dango.oauth.providers import (
        FacebookOAuthProvider,
        GoogleOAuthProvider,
        ShopifyOAuthProvider,
    )
    from dango.oauth.storage import OAuthStorage

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)
        oauth_storage = OAuthStorage(project_root)

        # Check if credential exists
        cred = oauth_storage.get(oauth_name)
        if not cred:
            console.print(f"\n[red]✗ OAuth credential '{oauth_name}' not found[/red]")
            console.print("\n[cyan]To see all credentials:[/cyan] dango auth list\n")
            raise click.Abort()

        # Show info
        console.print("\n🍡 [bold]Re-authenticating OAuth credential:[/bold]")
        console.print(f"  Source Type: {cred.source_type}")
        console.print(f"  Provider: {cred.provider}")
        console.print(f"  Account: {cred.account_info}\n")

        # Dispatch to appropriate provider
        oauth_manager = create_oauth_manager(project_root)
        new_oauth_name = None

        if cred.provider == "google":
            service = cred.metadata.get("service", "google_ads") if cred.metadata else "google_ads"
            google_provider = GoogleOAuthProvider(oauth_manager)
            new_oauth_name = google_provider.authenticate(service=service)

        elif cred.provider == "facebook_ads":
            facebook_provider = FacebookOAuthProvider(oauth_manager)
            new_oauth_name = facebook_provider.authenticate()

        elif cred.provider == "shopify":
            shopify_provider = ShopifyOAuthProvider(oauth_manager)
            new_oauth_name = shopify_provider.authenticate()
        else:
            console.print(f"\n[red]✗ Unsupported provider: {cred.provider}[/red]\n")
            raise click.Abort()

        if not new_oauth_name:
            console.print("\n[red]✗ Re-authentication failed[/red]\n")
            raise click.Abort()

        console.print("\n[green]✓ OAuth credential refreshed successfully[/green]")
        console.print(f"[dim]  New credential: {new_oauth_name}[/dim]\n")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("facebook_ads")
@click.pass_context
def auth_facebook_ads(ctx: click.Context) -> None:
    """
    Authenticate with Facebook Ads using OAuth.

    This will guide you through:
    1. Getting a short-lived access token from Facebook Graph API Explorer
    2. Exchanging it for a long-lived token (60 days)
    3. Credentials saved to .dlt/secrets.toml

    The token will need to be refreshed every 60 days.
    """
    from dango.oauth import OAuthManager
    from dango.oauth.providers import FacebookOAuthProvider

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        # Use new OAuth implementation
        oauth_manager = OAuthManager(project_root)
        provider = FacebookOAuthProvider(oauth_manager)

        # Start OAuth flow
        oauth_name = provider.authenticate()

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("google_sheets")
@click.pass_context
def auth_google_sheets(ctx: click.Context) -> None:
    """
    Authenticate with Google Sheets using OAuth.

    This will guide you through the browser-based OAuth flow:
    1. Create OAuth credentials in Google Cloud Console
    2. Authorize Dango via browser
    3. Credentials saved to .dlt/secrets.toml
    """
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        oauth_manager = OAuthManager(project_root)
        provider = GoogleOAuthProvider(oauth_manager)
        oauth_name = provider.authenticate(service="google_sheets")

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

        console.print("\n[dim]Note: If your GCP OAuth consent screen is in 'Testing' mode,")
        console.print("refresh tokens expire after 7 days. Publish your app for production use.")
        console.print("See: https://console.cloud.google.com/apis/credentials/consent[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("google_analytics")
@click.pass_context
def auth_google_analytics(ctx: click.Context) -> None:
    """
    Authenticate with Google Analytics (GA4) using OAuth.

    This will guide you through the browser-based OAuth flow:
    1. Create OAuth credentials in Google Cloud Console
    2. Authorize Dango via browser
    3. Credentials saved to .dlt/secrets.toml
    """
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        oauth_manager = OAuthManager(project_root)
        provider = GoogleOAuthProvider(oauth_manager)
        oauth_name = provider.authenticate(service="google_analytics")

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

        console.print("\n[dim]Note: If your GCP OAuth consent screen is in 'Testing' mode,")
        console.print("refresh tokens expire after 7 days. Publish your app for production use.")
        console.print("See: https://console.cloud.google.com/apis/credentials/consent[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("google_ads")
@click.pass_context
def auth_google_ads(ctx: click.Context) -> None:
    """
    Authenticate with Google Ads using OAuth.

    This will guide you through the browser-based OAuth flow:
    1. Create OAuth credentials in Google Cloud Console
    2. Authorize Dango via browser
    3. Credentials saved to .dlt/secrets.toml
    """
    from dango.oauth import OAuthManager
    from dango.oauth.providers import GoogleOAuthProvider

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        oauth_manager = OAuthManager(project_root)
        provider = GoogleOAuthProvider(oauth_manager)
        oauth_name = provider.authenticate(service="google_ads")

        if not oauth_name:
            console.print("[red]Authentication failed[/red]")
            raise click.Abort()

        console.print("\n[dim]Note: If your GCP OAuth consent screen is in 'Testing' mode,")
        console.print("refresh tokens expire after 7 days. Publish your app for production use.")
        console.print("See: https://console.cloud.google.com/apis/credentials/consent[/dim]")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("check")
@click.pass_context
def auth_check(ctx: click.Context) -> None:
    """
    Check OAuth configuration and credential status.

    Validates:
    - OAuth client credentials in .env
    - Saved OAuth tokens in .dlt/secrets.toml
    - Token expiry status

    Example:
      dango auth check
    """
    import os

    from dotenv import load_dotenv

    from dango.oauth.storage import OAuthStorage

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        # Load .env
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        console.print("\n[bold cyan]OAuth Configuration Check[/bold cyan]\n")

        # Define providers and their required env vars
        providers = {
            "Google": {
                "env_vars": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
                "services": ["Google Ads", "Google Analytics", "Google Sheets"],
                "auth_cmd": "dango auth google_<sheets|analytics|ads>",
            },
            "Facebook": {
                "env_vars": ["FACEBOOK_APP_ID", "FACEBOOK_APP_SECRET"],
                "services": ["Facebook Ads"],
                "auth_cmd": "dango auth facebook_ads",
            },
        }

        # Check each provider's env vars
        console.print("[bold]1. OAuth Client Credentials (.env)[/bold]\n")

        all_configured = True
        for provider_name, config in providers.items():
            env_vars = config["env_vars"]
            configured = all(os.getenv(var) for var in env_vars)

            if configured:
                console.print(f"  [green]✓[/green] {provider_name}")
                for var in env_vars:
                    value = os.getenv(var, "")
                    masked = value[:8] + "..." if len(value) > 8 else "***"
                    console.print(f"    [dim]{var}: {masked}[/dim]")
            else:
                console.print(f"  [red]✗[/red] {provider_name}")
                for var in env_vars:
                    if os.getenv(var):
                        console.print(f"    [green]✓[/green] {var}: configured")
                    else:
                        console.print(f"    [red]✗[/red] {var}: [dim]missing[/dim]")
                console.print("    [dim]→ Add credentials to .env file[/dim]")
                all_configured = False

        # Check saved OAuth tokens
        console.print("\n[bold]2. Saved OAuth Tokens (.dlt/secrets.toml)[/bold]\n")

        oauth_storage = OAuthStorage(project_root)
        credentials = oauth_storage.list()

        if not credentials:
            console.print("  [yellow]No OAuth tokens saved yet[/yellow]")
            console.print("  [dim]→ Run: dango auth <provider> to authenticate[/dim]")
        else:
            for cred in credentials:
                if cred.is_expired():
                    status = "[red]EXPIRED[/red]"
                    action = f"[dim]→ Run: dango auth refresh {cred.source_type}[/dim]"
                elif cred.is_expiring_soon():
                    days_left = cred.days_until_expiry()
                    status = f"[yellow]Expires in {days_left}d[/yellow]"
                    action = (
                        f"[dim]→ Consider refreshing: dango auth refresh {cred.source_type}[/dim]"
                    )
                else:
                    status = "[green]Active[/green]"
                    action = ""

                console.print(f"  {status} {cred.account_info}")
                console.print(
                    f"    [dim]Provider: {cred.provider} | Source: {cred.source_type}[/dim]"
                )
                if action:
                    console.print(f"    {action}")

        # Live token validation
        console.print("\n[bold]3. Live Token Validation[/bold]\n")

        if credentials:
            from dango.oauth.validation import validate_all_tokens

            validation_results = validate_all_tokens(project_root)
            has_google = False

            for vr in validation_results:
                if vr.provider == "google":
                    has_google = True

                if vr.valid:
                    if vr.error_code == "network_error":
                        console.print(f"  [yellow]?[/yellow] {vr.source_type} — {vr.message}")
                    else:
                        info = f" ({vr.account_info})" if vr.account_info else ""
                        console.print(f"  [green]✓[/green] {vr.source_type} — Token valid{info}")
                else:
                    console.print(f"  [red]✗[/red] {vr.source_type} — {vr.message}")

            # GCP Testing mode caveat
            if has_google:
                console.print(
                    "\n  [dim]Note: If your GCP OAuth app is in 'Testing' mode, refresh tokens"
                )
                console.print("  expire after 7 days. Publish your app for permanent tokens.")
                console.print(
                    "  See: https://console.cloud.google.com/apis/credentials/consent[/dim]"
                )
        else:
            console.print("  [dim]No tokens to validate[/dim]")

        # Summary and next steps
        console.print("\n[bold]4. Summary[/bold]\n")

        if all_configured and credentials:
            valid_count = sum(1 for vr in validation_results if vr.valid)
            invalid_count = sum(1 for vr in validation_results if not vr.valid)
            if invalid_count == 0:
                console.print("  [green]✓ OAuth is fully configured[/green]")
                console.print("  [dim]You can add OAuth sources with: dango source add[/dim]")
            else:
                console.print(
                    f"  [yellow]⚠️  {invalid_count} token(s) need re-authentication[/yellow]"
                )
                console.print(f"  [dim]{valid_count} valid, {invalid_count} invalid[/dim]")
        elif all_configured:
            console.print(
                "  [yellow]⚠️  OAuth credentials configured but not yet authenticated[/yellow]"
            )
            console.print("  [dim]Authenticate with: dango auth <provider>[/dim]")
        else:
            console.print("  [yellow]⚠️  Some OAuth credentials missing[/yellow]")
            console.print("  [dim]Add missing credentials to .env file[/dim]")

        console.print("")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e


@auth.command("setup")
@click.argument("provider", type=click.Choice(["google", "facebook"], case_sensitive=False))
@click.pass_context
def auth_setup(ctx: click.Context, provider: str) -> None:
    """
    Interactive OAuth setup wizard.

    Guides you through creating OAuth credentials for a provider.

    PROVIDER: The OAuth provider to set up (google, facebook)

    Examples:
      dango auth setup google
      dango auth setup facebook
    """
    import os

    import inquirer
    from dotenv import load_dotenv, set_key
    from inquirer import themes
    from rich.panel import Panel
    from rich.prompt import Confirm

    from ..utils import require_project_context

    try:
        project_root = require_project_context(ctx)

        # Load existing .env
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)

        console.print(f"\n[bold cyan]OAuth Setup Wizard: {provider.title()}[/bold cyan]\n")

        # Provider-specific configuration
        provider_config = {
            "google": {
                "display_name": "Google",
                "env_vars": [
                    ("GOOGLE_CLIENT_ID", "OAuth Client ID"),
                    ("GOOGLE_CLIENT_SECRET", "OAuth Client Secret"),
                ],
                "setup_url": "https://console.cloud.google.com/apis/credentials",
                "setup_steps": [
                    "1. Go to Google Cloud Console → APIs & Services → Credentials",
                    "2. Click '+ CREATE CREDENTIALS' → 'OAuth client ID'",
                    "3. Application type: 'Web application'",
                    "4. Name: 'Dango Local' (or any name)",
                    "5. Authorized redirect URIs: Add 'http://localhost:8080/callback'",
                    "6. Click 'Create' and copy the Client ID and Client Secret",
                ],
                "services": ["Google Ads", "Google Analytics", "Google Sheets"],
            },
            "facebook": {
                "display_name": "Facebook",
                "env_vars": [
                    ("FACEBOOK_APP_ID", "App ID"),
                    ("FACEBOOK_APP_SECRET", "App Secret"),
                ],
                "setup_url": "https://developers.facebook.com/apps/",
                "setup_steps": [
                    "1. Go to Facebook Developers → My Apps → Create App",
                    "2. Select 'Business' app type",
                    "3. Add 'Marketing API' product",
                    "4. Go to Settings → Basic to get App ID and App Secret",
                    "5. Add 'http://localhost:8080/callback' to Valid OAuth Redirect URIs",
                ],
                "services": ["Facebook Ads"],
            },
        }

        config = provider_config[provider.lower()]

        # Show privacy message
        console.print(
            Panel(
                "[bold]Why create your own OAuth app?[/bold]\n\n"
                "• Your data flows directly: Provider → Your Machine → Local Database\n"
                "• Dango never touches your data (no intermediary servers)\n"
                "• You control the OAuth app and can revoke access anytime\n"
                "• No shared rate limits or quotas",
                title="Privacy First",
                border_style="green",
            )
        )

        # Check if already configured
        all_configured = all(os.getenv(var) for var, _ in config["env_vars"])
        if all_configured:
            console.print(
                f"\n[green]✓ {config['display_name']} OAuth credentials already configured[/green]"
            )

            if not Confirm.ask("Update credentials anyway?", default=False):
                console.print("\n[dim]To authenticate, run:[/dim]")
                if provider.lower() == "google":
                    console.print("  dango auth google_sheets")
                    console.print("  dango auth google_analytics")
                    console.print("  dango auth google_ads")
                else:
                    console.print(f"  dango auth {provider.lower()}")
                return

        # Show setup steps
        console.print(f"\n[bold]Setup Steps for {config['display_name']}:[/bold]\n")
        for step in config["setup_steps"]:
            console.print(f"  {step}")

        console.print(f"\n[cyan]Setup URL:[/cyan] {config['setup_url']}\n")

        # Ask if ready to continue
        if not Confirm.ask("Ready to enter credentials?", default=True):
            console.print("\n[yellow]Setup cancelled[/yellow]")
            console.print(f"[dim]Run again when ready: dango auth setup {provider.lower()}[/dim]")
            return

        # Collect credentials
        console.print("\n[bold]Enter your OAuth credentials:[/bold]\n")

        credentials = {}
        for env_var, display_name in config["env_vars"]:
            current_value = os.getenv(env_var, "")
            if current_value:
                masked = current_value[:8] + "..." if len(current_value) > 8 else "***"
                console.print(f"  [dim]Current {display_name}: {masked}[/dim]")

            questions = [
                inquirer.Text(
                    env_var,
                    message=display_name,
                    default="" if not current_value else None,
                )
            ]
            answers = inquirer.prompt(questions, theme=themes.GreenPassion())
            if not answers or not answers[env_var]:
                if current_value:
                    console.print("  [dim]Keeping existing value[/dim]")
                    credentials[env_var] = current_value
                else:
                    console.print(f"\n[red]✗ {display_name} is required[/red]")
                    raise click.Abort()
            else:
                credentials[env_var] = answers[env_var]

        # Save to .env
        console.print("\n[dim]Saving credentials to .env...[/dim]")

        # Create .env if doesn't exist
        if not env_file.exists():
            env_file.touch()

        for env_var, value in credentials.items():
            set_key(str(env_file), env_var, value)

        console.print(
            f"\n[green]✓ {config['display_name']} OAuth credentials saved to .env[/green]"
        )

        # Next steps
        console.print("\n[bold]Next Steps:[/bold]")
        console.print("  1. Authenticate: ", end="")
        if provider.lower() == "google":
            console.print("[cyan]dango auth <google_ads|google_analytics|google_sheets>[/cyan]")
        else:
            console.print(f"[cyan]dango auth {provider.lower()}[/cyan]")
        console.print("  2. Add a source: [cyan]dango source add[/cyan]")
        console.print("")

    except KeyboardInterrupt:
        console.print("\n[yellow]Setup cancelled[/yellow]")
        raise click.Abort() from None
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        from dango.exceptions import is_debug_mode

        if is_debug_mode():
            import traceback

            console.print(traceback.format_exc())
        raise click.Abort() from e
