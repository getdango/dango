"""
OAuth Provider Implementations

Provider-specific OAuth flows for data sources.
Each provider class handles:
- Building authorization URLs
- Exchanging authorization codes for tokens
- Saving credentials in the correct format for dlt

Providers:
- GoogleOAuthProvider: Google Ads, Analytics, Sheets (shared OAuth)
- FacebookOAuthProvider: Facebook/Meta Ads
- ShopifyOAuthProvider: Shopify stores
"""

import requests
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
from datetime import datetime, timedelta

from rich.console import Console
from rich.prompt import Prompt, Confirm
from rich.panel import Panel

from dango.oauth import OAuthManager
from dango.oauth.storage import OAuthStorage, OAuthCredential

console = Console()


def _clean_pasted_input(value: str) -> str:
    """
    Clean pasted input by removing newlines and extra whitespace.

    This handles the common case where users accidentally copy trailing
    newlines when pasting values from websites or text editors.
    """
    if not value:
        return ""
    # Remove newlines, carriage returns, and strip whitespace
    return value.replace("\n", "").replace("\r", "").strip()


class BaseOAuthProvider:
    """Base class for OAuth providers"""

    def __init__(self, oauth_manager: OAuthManager):
        """
        Initialize provider

        Args:
            oauth_manager: OAuth manager instance
        """
        self.oauth_manager = oauth_manager
        self.project_root = oauth_manager.project_root
        self.oauth_storage = OAuthStorage(self.project_root)

    def authenticate(self, source_name: Optional[str] = None) -> Optional[str]:
        """
        Run OAuth flow

        Args:
            source_name: Optional source name for instance-specific credentials

        Returns:
            OAuth credential name if successful, None otherwise
        """
        raise NotImplementedError("Subclasses must implement authenticate()")


class GoogleOAuthProvider(BaseOAuthProvider):
    """
    Google OAuth Provider

    Supports Google Ads, Google Analytics, and Google Sheets.
    All use the same OAuth credentials (with different scopes).

    Uses dlt's GcpOAuthCredentials format.
    """

    # OAuth endpoints
    AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_URL = "https://oauth2.googleapis.com/token"

    # Scopes for different Google services
    # Always include userinfo.email to identify the authenticated user
    BASE_SCOPES = [
        "https://www.googleapis.com/auth/userinfo.email",
    ]

    SCOPES = {
        "google_ads": [
            "https://www.googleapis.com/auth/adwords"
        ],
        "google_analytics": [
            "https://www.googleapis.com/auth/analytics.readonly"
        ],
        "google_sheets": [
            "https://www.googleapis.com/auth/spreadsheets.readonly"
            # Note: Drive scope removed - we only read specific spreadsheets via Sheets API,
            # don't need access to all Drive files
        ],
    }

    def authenticate(self, service: str = "google_ads", source_name: Optional[str] = None) -> Optional[str]:
        """
        Run Google OAuth flow

        Args:
            service: Service to authenticate (google_ads, google_analytics, google_sheets)
            source_name: Optional source name (not used for Google - uses email as identifier)

        Returns:
            OAuth credential name if successful, None otherwise
        """
        import os
        from dotenv import load_dotenv

        try:
            console.print(f"\n[bold cyan]Google {service.replace('_', ' ').title()} Authentication[/bold cyan]\n")

            # Try to load credentials from .env first
            env_file = self.project_root / ".env"
            load_dotenv(env_file, override=True)
            client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
            client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

            # Debug: check if .env exists and has the keys
            if not client_id and env_file.exists():
                # Try reading directly from file as fallback
                with open(env_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("GOOGLE_CLIENT_ID="):
                            client_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("GOOGLE_CLIENT_SECRET="):
                            client_secret = line.split("=", 1)[1].strip().strip('"').strip("'")

            if client_id and client_secret:
                # Credentials found in .env
                console.print("[green]✓ Found OAuth credentials in .env[/green]")
                console.print(f"[dim]  Client ID: {client_id[:20]}...{client_id[-10:]}[/dim]\n")
            else:
                # Show setup instructions only if credentials not found
                instructions = [
                    "[bold]Prerequisites:[/bold]",
                    "1. Create a Google Cloud Project at https://console.cloud.google.com/",
                    f"2. Enable the required API (Google Ads API / Analytics API / Sheets API)",
                    "3. Create OAuth 2.0 credentials:",
                    "   • Go to APIs & Services > Credentials",
                    "   • Create OAuth client ID",
                    "   • Application type: [yellow]Web application[/yellow] (NOT Desktop app)",
                    f"   • Authorized redirect URI: {self.oauth_manager.callback_url}",
                    "4. Download or copy the Client ID and Client Secret",
                    "",
                    "[dim]Tip: Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env to skip this step[/dim]",
                ]

                console.print(Panel("\n".join(instructions), title="Setup Instructions", border_style="cyan"))

                # Get OAuth client credentials
                console.print("\n[bold]Enter OAuth Client Credentials:[/bold]")
                client_id = _clean_pasted_input(Prompt.ask("Client ID"))
                client_secret = _clean_pasted_input(Prompt.ask("Client Secret", password=True))

                # Save to .env for future Google services (shared credentials)
                if client_id and client_secret:
                    from dotenv import set_key
                    if not env_file.exists():
                        env_file.touch()
                    set_key(str(env_file), "GOOGLE_CLIENT_ID", client_id)
                    set_key(str(env_file), "GOOGLE_CLIENT_SECRET", client_secret)
                    console.print("[dim]Saved credentials to .env for future use[/dim]")

            if not client_id or not client_secret:
                console.print("[red]✗ Client ID and Secret are required[/red]")
                return False

            # Build authorization URL
            # Combine base scopes (userinfo) with service-specific scopes
            service_scopes = self.SCOPES.get(service, self.SCOPES["google_ads"])
            all_scopes = self.BASE_SCOPES + service_scopes
            state = self.oauth_manager.generate_state()

            auth_params = {
                "client_id": client_id,
                "redirect_uri": self.oauth_manager.callback_url,
                "response_type": "code",
                "scope": " ".join(all_scopes),
                "access_type": "offline",  # Request refresh token
                "prompt": "consent",  # Force consent screen to get refresh token
                "state": state,
            }

            auth_url = f"{self.AUTH_URL}?{urlencode(auth_params)}"

            # Start OAuth flow
            console.print("\n[bold]Step 2: Authorize Dango[/bold]")
            oauth_response = self.oauth_manager.start_oauth_flow("Google", auth_url)

            if not oauth_response:
                console.print("[red]✗ OAuth flow failed or timed out[/red]")
                return False

            # Verify state parameter
            if oauth_response.get('state') != state:
                console.print("[red]✗ Invalid state parameter (possible CSRF attack)[/red]")
                return False

            # Exchange authorization code for tokens
            console.print("\n[cyan]Exchanging authorization code for tokens...[/cyan]")
            tokens = self._exchange_code_for_tokens(
                code=oauth_response['code'],
                client_id=client_id,
                client_secret=client_secret,
            )

            if not tokens:
                console.print("[red]✗ Token exchange failed[/red]")
                return None

            # Fetch user info to get email (identifier)
            console.print("\n[cyan]Fetching user info...[/cyan]")
            user_info = self._fetch_user_info(tokens['access_token'])

            if not user_info or 'email' not in user_info:
                console.print("[red]✗ Could not get user email[/red]")
                return None

            email = user_info['email']

            # Save credentials in dlt format
            # Include impersonated_email for Google Ads (required by dlt function signature)
            # project_id is required by dlt's GcpOAuthCredentials - use placeholder
            credentials = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": tokens['refresh_token'],
                "project_id": "dango-oauth",  # Required by dlt GcpOAuthCredentials
                "impersonated_email": email,  # Used by Google Ads
            }

            # For Google Ads, also ask for developer token and customer ID
            if service == "google_ads":
                console.print("\n[bold]Step 3: Google Ads Specific Credentials[/bold]")
                console.print("[dim]Find your Developer Token at: https://ads.google.com/aw/apicenter[/dim]")
                console.print("[dim]Note: You need a Google Ads Manager Account to get a Developer Token[/dim]")

                # Collect and confirm Google Ads credentials
                while True:
                    dev_token = _clean_pasted_input(Prompt.ask("Developer Token (required for sync, press Enter to skip for now)", default=""))
                    customer_id = _clean_pasted_input(Prompt.ask("Customer ID (digits only, no dashes, e.g., 1234567890)", default=""))
                    customer_id = customer_id.replace("-", "")

                    # Show summary for confirmation
                    console.print("\n[bold]Please verify:[/bold]")
                    if dev_token:
                        console.print(f"  Developer Token: {dev_token[:8]}...{dev_token[-4:]}")
                    else:
                        console.print("  Developer Token: [dim][skipped][/dim]")
                    console.print(f"  Customer ID: {customer_id or '[dim][skipped][/dim]'}")

                    if Confirm.ask("\n[cyan]Is this correct?[/cyan]", default=True):
                        break

                    console.print("\n[yellow]Let's re-enter these values:[/yellow]")

                # Store in credentials dict - storage.py will write as sibling fields
                if dev_token:
                    credentials["dev_token"] = dev_token
                if customer_id:
                    credentials["customer_id"] = customer_id

            # For Google Analytics, property_id is collected during source wizard
            elif service == "google_analytics":
                console.print("\n[bold]Step 3: Google Analytics Configuration[/bold]")
                console.print("[dim]You'll enter the GA4 Property ID when adding the source[/dim]")

            # For Google Sheets, ask for spreadsheet ID
            elif service == "google_sheets":
                console.print("\n[bold]Step 3: Google Sheets Configuration[/bold]")
                console.print("[dim]You can add spreadsheet IDs later when adding the source[/dim]")

            # Create OAuth credential with metadata
            # Use email only if name not available (avoid "Unknown")
            name = user_info.get('name')
            account_info = f"{name} ({email})" if name else email
            oauth_cred = OAuthCredential(
                source_type=service,  # e.g., "google_ads", "google_sheets"
                provider="google",
                identifier=email,
                account_info=account_info,
                credentials=credentials,
                created_at=datetime.now(),
                expires_at=None,  # Google refresh tokens don't expire
                metadata={
                    "scopes": all_scopes
                }
            )

            # Save using new storage - writes to sources.{service}.credentials.*
            if not self.oauth_storage.save(oauth_cred):
                return None

            # Success message
            console.print(f"\n[green]✅ Google authentication complete![/green]\n")
            console.print(f"[cyan]OAuth credentials saved for:[/cyan] {service}")
            console.print("[cyan]Next steps:[/cyan]")
            console.print(f"  1. Add Google source: [bold]dango source add[/bold]")
            console.print(f"  2. Select '{service.replace('_', ' ').title()}' from the wizard")
            console.print("  3. Run sync to load data")

            return service  # Return source_type, not oauth_name

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return None
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            import traceback
            traceback.print_exc()
            return None

    def _exchange_code_for_tokens(
        self,
        code: str,
        client_id: str,
        client_secret: str
    ) -> Optional[Dict[str, str]]:
        """
        Exchange authorization code for access and refresh tokens

        Args:
            code: Authorization code from OAuth callback
            client_id: OAuth client ID
            client_secret: OAuth client secret

        Returns:
            Dictionary with access_token and refresh_token, or None if failed
        """
        try:
            token_data = {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": self.oauth_manager.callback_url,
                "grant_type": "authorization_code",
            }

            response = requests.post(self.TOKEN_URL, data=token_data)
            response.raise_for_status()

            tokens = response.json()

            if 'refresh_token' not in tokens:
                console.print("[yellow]⚠️  No refresh token received.[/yellow]")
                console.print("[yellow]   This usually means you've authorized this app before.[/yellow]")
                console.print("[yellow]   To get a refresh token, revoke access at:[/yellow]")
                console.print("[yellow]   https://myaccount.google.com/permissions[/yellow]")
                console.print("[yellow]   Then try again.[/yellow]")
                return None

            console.print("[green]✓ Tokens received successfully![/green]")
            return tokens

        except requests.exceptions.RequestException as e:
            console.print(f"[red]✗ Token exchange failed: {e}[/red]")
            if hasattr(e.response, 'text'):
                console.print(f"[red]Response: {e.response.text}[/red]")
            return None

    def _fetch_user_info(self, access_token: str) -> Optional[Dict[str, Any]]:
        """
        Fetch user info from Google (email, etc.)

        Args:
            access_token: Access token from OAuth flow

        Returns:
            Dictionary with user info (email, name, etc.) or None if failed
        """
        try:
            user_info_url = "https://www.googleapis.com/oauth2/v1/userinfo"
            headers = {"Authorization": f"Bearer {access_token}"}

            response = requests.get(user_info_url, headers=headers)
            response.raise_for_status()

            user_info = response.json()
            console.print(f"[dim]Authenticated as: {user_info.get('email')}[/dim]")
            return user_info

        except requests.exceptions.RequestException as e:
            console.print(f"[yellow]⚠️  Could not fetch user info: {e}[/yellow]")
            return None

class FacebookOAuthProvider(BaseOAuthProvider):
    """
    Facebook/Meta Ads OAuth Provider

    Uses long-lived tokens (60-day expiry).
    For simplicity, we still use the manual token exchange method
    since Facebook OAuth for Marketing API is complex.
    """

    # Token exchange endpoint
    TOKEN_EXCHANGE_URL = "https://graph.facebook.com/v18.0/oauth/access_token"

    def authenticate(self, source_name: Optional[str] = None) -> Optional[str]:
        """
        Run Facebook OAuth flow

        For MVP, we use the simpler approach of exchanging
        short-lived tokens for long-lived tokens.

        If a valid (non-expired) token exists with stored app credentials,
        offers to auto-extend the token for another 60 days.

        Args:
            source_name: Optional source name (not used - uses account_id as identifier)

        Returns:
            OAuth credential name if successful, None otherwise
        """
        try:
            console.print("\n[bold cyan]Facebook Ads Authentication[/bold cyan]\n")

            # Check for existing credentials
            existing_cred = self.oauth_storage.get("facebook_ads")
            if existing_cred:
                if existing_cred.is_expired():
                    # Token is expired - inform user and proceed with re-auth
                    expired_date = existing_cred.expires_at.strftime('%Y-%m-%d') if existing_cred.expires_at else 'unknown'
                    console.print(f"[red]⚠️  Your Facebook token has expired ({expired_date})[/red]")
                    console.print(f"[dim]Account: {existing_cred.account_info or existing_cred.identifier}[/dim]")
                    console.print("[cyan]Let's get you a new token...[/cyan]\n")
                else:
                    # Token is still valid - check for auto-extend capability
                    app_id = existing_cred.metadata.get("app_id") if existing_cred.metadata else None
                    app_secret = existing_cred.metadata.get("app_secret") if existing_cred.metadata else None
                    current_token = existing_cred.credentials.get("access_token") if existing_cred.credentials else None

                    if app_id and app_secret and current_token:
                        days_left = existing_cred.days_until_expiry()
                        console.print(f"[cyan]Found existing valid token (expires in {days_left} days)[/cyan]")
                        console.print(f"[dim]Account: {existing_cred.account_info or existing_cred.identifier}[/dim]\n")

                        if Confirm.ask("[cyan]Extend token for another 60 days?[/cyan]", default=True):
                            # Attempt auto-extend
                            console.print("\n[cyan]Exchanging token for new 60-day token...[/cyan]")
                            new_token = self._exchange_token(current_token, app_id, app_secret)

                            if new_token:
                                # Update credentials with new token and expiry
                                existing_cred.credentials["access_token"] = new_token
                                existing_cred.expires_at = datetime.now() + timedelta(days=60)
                                existing_cred.created_at = datetime.now()

                                if self.oauth_storage.save(existing_cred):
                                    console.print(f"\n[green]✅ Token extended successfully![/green]")
                                    console.print(f"[yellow]New expiry:[/yellow] {existing_cred.expires_at.strftime('%Y-%m-%d')} (60 days)")
                                    return "facebook_ads"
                                else:
                                    console.print("[red]✗ Failed to save extended token[/red]")
                            else:
                                console.print("[yellow]Auto-extend failed (token may have been invalidated).[/yellow]")
                                console.print("[dim]Falling back to manual re-authentication...[/dim]\n")
                        else:
                            console.print("[dim]Proceeding with full re-authentication...[/dim]\n")
                    elif app_id and current_token and not app_secret:
                        # Legacy credentials without app_secret - inform user
                        console.print("[yellow]Found existing token but missing app credentials for auto-extend.[/yellow]")
                        console.print("[dim]After this re-authentication, future extends will be automatic.[/dim]\n")

            # Show instructions for manual flow
            instructions = [
                "[bold]Steps to get access token:[/bold]",
                "1. Go to: https://developers.facebook.com/tools/explorer/",
                "2. Select your app (or create one at developers.facebook.com/apps)",
                "3. Click 'Generate Access Token'",
                "4. Grant permissions: [yellow]ads_read, ads_management[/yellow]",
                "5. Copy the short-lived access token (expires in 1-2 hours)",
                "",
                "[dim]We'll exchange this for a 60-day long-lived token[/dim]",
            ]

            console.print(Panel("\n".join(instructions), title="Setup Instructions", border_style="cyan"))

            # Get short-lived token
            console.print("\n[bold]Step 1: Short-lived Access Token[/bold]")
            short_token = _clean_pasted_input(Prompt.ask("Paste short-lived access token"))

            if not short_token:
                console.print("[red]✗ Access token is required[/red]")
                return None

            console.print(f"[dim]  Captured: {short_token[:15]}...{short_token[-8:]}[/dim]")

            # Get App credentials
            console.print("\n[bold]Step 2: App Credentials[/bold]")
            console.print("[dim]Find at: developers.facebook.com/apps → Your App → Settings → Basic[/dim]")

            app_id = _clean_pasted_input(Prompt.ask("Facebook App ID"))
            console.print("[dim]Click 'Show' next to App Secret to reveal it[/dim]")
            app_secret = _clean_pasted_input(Prompt.ask("Facebook App Secret", password=True))

            if not app_id or not app_secret:
                console.print("[red]✗ App ID and Secret are required[/red]")
                return None

            # Exchange for long-lived token
            console.print("\n[cyan]Exchanging for long-lived token (60 days)...[/cyan]")
            long_token = self._exchange_token(short_token, app_id, app_secret)

            if not long_token:
                console.print("[red]✗ Token exchange failed[/red]")
                return None

            # Get Ad Account ID with confirmation
            console.print("\n[bold]Step 3: Ad Account ID[/bold]")
            console.print("[dim]Go to adsmanager.facebook.com → Look at URL for act=XXXXX or click account dropdown[/dim]")

            while True:
                account_id = _clean_pasted_input(Prompt.ask("Ad Account ID (e.g., 123456789)"))

                if not account_id:
                    console.print("[red]✗ Account ID is required[/red]")
                    continue

                # Normalize account_id (remove "act_" prefix if present for consistency)
                account_id_clean = account_id.replace("act_", "")

                console.print(f"\n[bold]Please verify:[/bold]")
                console.print(f"  Account ID: {account_id_clean}")

                if Confirm.ask("\n[cyan]Is this correct?[/cyan]", default=True):
                    break

                console.print("\n[yellow]Let's re-enter:[/yellow]")

            # Save credentials - store clean account_id without "act_" prefix
            credentials = {
                "access_token": long_token,
                "account_id": account_id_clean  # Clean ID - helpers.py will add "act_" prefix
            }

            # Create OAuth credential with metadata
            # Store app_id and app_secret to enable auto-extend in future
            expires_at = datetime.now() + timedelta(days=60)
            oauth_cred = OAuthCredential(
                source_type="facebook_ads",
                provider="facebook_ads",
                identifier=account_id_clean,
                account_info=f"Facebook Ads Account ({account_id})",
                credentials=credentials,
                created_at=datetime.now(),
                expires_at=expires_at,
                metadata={
                    "app_id": app_id,
                    "app_secret": app_secret  # Stored for token auto-extend
                }
            )

            # Save using new storage - writes to sources.facebook_ads.credentials.*
            if not self.oauth_storage.save(oauth_cred):
                return None

            # Success message
            console.print(f"\n[green]✅ Facebook Ads authentication complete![/green]\n")
            console.print(f"[cyan]OAuth credentials saved for:[/cyan] facebook_ads")
            console.print(f"[yellow]Token expires:[/yellow] {expires_at.strftime('%Y-%m-%d')} (60 days)")
            console.print("\n[cyan]Next steps:[/cyan]")
            console.print("  1. Add Facebook Ads source: [bold]dango source add[/bold]")
            console.print("  2. Select 'Facebook Ads' from the wizard")
            console.print("  3. Run sync to load data")
            console.print("\n[yellow]⚠️  Set a reminder to re-authenticate before expiry[/yellow]")

            return "facebook_ads"  # Return source_type

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return None
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return None

    def _exchange_token(self, short_token: str, app_id: str, app_secret: str) -> Optional[str]:
        """
        Exchange short-lived token for long-lived token

        Args:
            short_token: Short-lived access token
            app_id: Facebook App ID
            app_secret: Facebook App Secret

        Returns:
            Long-lived access token or None if failed
        """
        try:
            params = {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            }

            response = requests.get(self.TOKEN_EXCHANGE_URL, params=params)
            response.raise_for_status()

            data = response.json()
            long_token = data.get("access_token")

            if long_token:
                console.print("[green]✓ Long-lived token obtained (valid for ~60 days)[/green]")
                return long_token
            else:
                console.print("[red]✗ No access_token in response[/red]")
                return None

        except requests.exceptions.RequestException as e:
            console.print(f"[red]✗ Token exchange failed: {e}[/red]")
            if hasattr(e.response, 'text'):
                console.print(f"[red]Response: {e.response.text}[/red]")
            return None


class ShopifyOAuthProvider(BaseOAuthProvider):
    """
    Shopify OAuth Provider

    Uses custom app private access tokens for simplicity.
    Full OAuth app flow is complex and requires app review.
    """

    def authenticate(self, source_name: Optional[str] = None) -> Optional[str]:
        """
        Run Shopify authentication (custom app method)

        Args:
            source_name: Optional source name (not used - uses shop_url as identifier)

        Returns:
            OAuth credential name if successful, None otherwise
        """
        try:
            console.print("\n[bold cyan]Shopify Authentication[/bold cyan]\n")

            # Show instructions
            instructions = [
                "[bold]Steps to create a custom app:[/bold]",
                "1. Go to your Shopify admin panel",
                "2. Settings > Apps and sales channels > Develop apps",
                "3. Click 'Create an app'",
                "4. Configure Admin API scopes (read permissions you need)",
                "5. Install the app on your store",
                "6. Reveal and copy the Admin API access token",
                "",
                "[dim]This creates a permanent access token for your store[/dim]",
            ]

            console.print(Panel("\n".join(instructions), title="Setup Instructions", border_style="cyan"))

            # Get shop URL
            console.print("\n[bold]Step 1: Shop Information[/bold]")
            shop_url = _clean_pasted_input(Prompt.ask("Shop URL (e.g., mystore.myshopify.com)"))

            # Normalize shop URL
            if not shop_url.endswith(".myshopify.com"):
                shop_url = f"{shop_url}.myshopify.com"

            # Get access token
            console.print("\n[bold]Step 2: Admin API Access Token[/bold]")
            access_token = _clean_pasted_input(Prompt.ask("Admin API access token", password=True))

            if not shop_url or not access_token:
                console.print("[red]✗ Shop URL and access token are required[/red]")
                return None

            # Test the credentials and get shop name
            console.print("\n[cyan]Testing connection...[/cyan]")
            shop_name = self._test_connection(shop_url, access_token)
            if not shop_name:
                console.print("[red]✗ Connection test failed[/red]")
                console.print("[yellow]Please verify your shop URL and access token[/yellow]")
                return None

            # Save credentials
            credentials = {
                "private_app_password": access_token,
                "shop_url": shop_url
            }

            # Create OAuth credential with metadata
            # Use shop_url without .myshopify.com as identifier
            shop_identifier = shop_url.replace(".myshopify.com", "").replace(".", "_")
            oauth_cred = OAuthCredential(
                source_type="shopify",
                provider="shopify",
                identifier=shop_identifier,
                account_info=f"{shop_name} ({shop_url})",
                credentials=credentials,
                created_at=datetime.now(),
                expires_at=None,  # Shopify custom app tokens don't expire
                metadata={
                    "shop_url": shop_url
                }
            )

            # Save using new storage - writes to sources.shopify.credentials.*
            if not self.oauth_storage.save(oauth_cred):
                return None

            # Success message
            console.print(f"\n[green]✅ Shopify authentication complete![/green]\n")
            console.print(f"[cyan]OAuth credentials saved for:[/cyan] shopify")
            console.print(f"[cyan]Shop:[/cyan] {shop_name} ({shop_url})")
            console.print("\n[cyan]Next steps:[/cyan]")
            console.print("  1. Add Shopify source: [bold]dango source add[/bold]")
            console.print("  2. Select 'Shopify' from the wizard")
            console.print("  3. Run sync to load data")

            return "shopify"  # Return source_type

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return None
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return None

    def _test_connection(self, shop_url: str, access_token: str) -> Optional[str]:
        """
        Test Shopify connection and get shop name

        Args:
            shop_url: Shop URL
            access_token: Admin API access token

        Returns:
            Shop name if connection successful, None otherwise
        """
        try:
            # Test with shop info endpoint
            url = f"https://{shop_url}/admin/api/2024-01/shop.json"
            headers = {
                "X-Shopify-Access-Token": access_token
            }

            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            shop_data = response.json()
            shop_name = shop_data.get("shop", {}).get("name", "Unknown")

            console.print(f"[green]✓ Connected to shop: {shop_name}[/green]")
            return shop_name

        except requests.exceptions.RequestException as e:
            console.print(f"[red]✗ Connection test failed: {e}[/red]")
            return None
