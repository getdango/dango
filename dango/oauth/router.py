"""
OAuth Router

Routes OAuth authentication requests to the correct provider based on source type.
Used by the source wizard for inline OAuth during source configuration.
"""

from pathlib import Path
from typing import Optional
from rich.console import Console

from dango.oauth import OAuthManager
from dango.oauth.providers import GoogleOAuthProvider, FacebookOAuthProvider, ShopifyOAuthProvider

console = Console()

# Map source types to OAuth providers and services
OAUTH_PROVIDER_MAP = {
    "google_ads": ("google", "google_ads"),
    "google_analytics": ("google", "google_analytics"),
    "google_sheets": ("google", "google_sheets"),
    "facebook_ads": ("facebook", None),
    "shopify": ("shopify", None),
}


def run_oauth_for_source(source_type: str, source_name: str, project_root: Path) -> bool:
    """
    Run OAuth authentication for a specific source instance.

    This is the main entry point for inline OAuth during source wizard.
    Saves credentials to [sources.{source_name}] in .dlt/secrets.toml,
    enabling multi-account setups (e.g., facebook_us, facebook_eu).

    Args:
        source_type: Source type key (e.g., "google_ads", "facebook_ads")
        source_name: Source instance name (e.g., "facebook_us")
        project_root: Path to project root

    Returns:
        True if OAuth successful, False otherwise
    """
    if source_type not in OAUTH_PROVIDER_MAP:
        console.print(f"[yellow]⚠️  No OAuth provider configured for '{source_type}'[/yellow]")
        return False

    provider_name, service = OAUTH_PROVIDER_MAP[source_type]

    # Create OAuth manager
    oauth_manager = OAuthManager(project_root)

    # Route to correct provider
    try:
        if provider_name == "google":
            provider = GoogleOAuthProvider(oauth_manager)
            # Pass source_name for instance-specific credentials
            return provider.authenticate(service=service, source_name=source_name)

        elif provider_name == "facebook":
            provider = FacebookOAuthProvider(oauth_manager)
            # Pass source_name for instance-specific credentials
            return provider.authenticate(source_name=source_name)

        elif provider_name == "shopify":
            provider = ShopifyOAuthProvider(oauth_manager)
            # Pass source_name for instance-specific credentials
            return provider.authenticate(source_name=source_name)

        else:
            console.print(f"[red]❌ Unknown OAuth provider: {provider_name}[/red]")
            return False

    except Exception as e:
        console.print(f"[red]❌ OAuth authentication failed: {e}[/red]")
        return False


def check_oauth_credentials_exist(source_type: str, source_name: str, project_root: Path) -> bool:
    """
    Check if OAuth credentials already exist for a source instance.

    Checks both instance-specific credentials [sources.{source_name}]
    and shared provider credentials [sources.{source_type}].

    Args:
        source_type: Source type key (e.g., "google_ads")
        source_name: Source instance name (e.g., "google_ads_us")
        project_root: Path to project root

    Returns:
        True if credentials exist, False otherwise
    """
    from dango.config.credentials import CredentialManager

    cred_manager = CredentialManager(project_root)

    try:
        # Load .dlt/secrets.toml
        secrets = cred_manager.load_secrets()

        # Check if source has credentials
        if "sources" not in secrets:
            return False

        # Check instance-specific credentials first (highest priority)
        if source_name in secrets["sources"]:
            source_creds = secrets["sources"][source_name]

            # Check for required OAuth fields based on source type
            if source_type in ["google_ads", "google_analytics", "google_sheets"]:
                if all(k in source_creds for k in ["client_id", "client_secret", "refresh_token"]):
                    return True
            elif source_type == "facebook_ads":
                if "access_token" in source_creds:
                    return True
            elif source_type == "shopify":
                if "private_app_password" in source_creds:
                    return True

        # Check shared provider credentials (fallback)
        # For Google services, check for shared Google OAuth credentials
        if source_type in ["google_ads", "google_analytics", "google_sheets"]:
            # Check if any Google OAuth creds exist
            for google_source in ["google_ads", "google_analytics", "google_sheets"]:
                if google_source in secrets["sources"]:
                    source_creds = secrets["sources"][google_source]
                    # Check for OAuth fields (client_id, client_secret, refresh_token)
                    if all(k in source_creds for k in ["client_id", "client_secret", "refresh_token"]):
                        return True
            return False

        # For Facebook Ads - check shared credentials
        elif source_type == "facebook_ads":
            if "facebook_ads" in secrets["sources"]:
                return "access_token" in secrets["sources"]["facebook_ads"]
            return False

        # For Shopify - check shared credentials
        elif source_type == "shopify":
            if "shopify" in secrets["sources"]:
                return "private_app_password" in secrets["sources"]["shopify"]
            return False

        else:
            return False

    except Exception:
        # If error loading secrets, assume credentials don't exist
        return False


def get_oauth_status_message(source_type: str, project_root: Path) -> Optional[str]:
    """
    Get a status message about OAuth credentials for a source.

    Args:
        source_type: Source type key
        project_root: Path to project root

    Returns:
        Status message or None if not applicable
    """
    if source_type not in OAUTH_PROVIDER_MAP:
        return None

    if check_oauth_credentials_exist(source_type, project_root):
        return f"[green]✓ OAuth credentials already configured[/green]"
    else:
        return f"[yellow]⚠️  OAuth credentials not found - setup required[/yellow]"
