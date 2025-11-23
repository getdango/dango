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

console = Console()


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

    def authenticate(self) -> bool:
        """
        Run OAuth flow

        Returns:
            True if authentication successful, False otherwise
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
    SCOPES = {
        "google_ads": [
            "https://www.googleapis.com/auth/adwords"
        ],
        "google_analytics": [
            "https://www.googleapis.com/auth/analytics.readonly"
        ],
        "google_sheets": [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly"
        ],
    }

    def authenticate(self, service: str = "google_ads") -> bool:
        """
        Run Google OAuth flow

        Args:
            service: Service to authenticate (google_ads, google_analytics, google_sheets)

        Returns:
            True if successful, False otherwise
        """
        try:
            console.print(f"\n[bold cyan]Google {service.replace('_', ' ').title()} Authentication[/bold cyan]\n")

            # Show setup instructions
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
            ]

            console.print(Panel("\n".join(instructions), title="Setup Instructions", border_style="cyan"))

            if Confirm.ask("\n[cyan]Open Google Cloud Console?[/cyan]", default=True):
                import webbrowser
                webbrowser.open("https://console.cloud.google.com/apis/credentials")

            # Get OAuth client credentials
            console.print("\n[bold]Step 1: OAuth Client Credentials[/bold]")
            client_id = Prompt.ask("Enter OAuth Client ID").strip()
            client_secret = Prompt.ask("Enter Client Secret", password=True).strip()

            if not client_id or not client_secret:
                console.print("[red]✗ Client ID and Secret are required[/red]")
                return False

            # Build authorization URL
            scopes = self.SCOPES.get(service, self.SCOPES["google_ads"])
            state = self.oauth_manager.generate_state()

            auth_params = {
                "client_id": client_id,
                "redirect_uri": self.oauth_manager.callback_url,
                "response_type": "code",
                "scope": " ".join(scopes),
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
                return False

            # Save credentials in dlt format
            credentials = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": tokens['refresh_token'],
                "project_id": ""  # Optional, can be added later
            }

            # For Google Ads, also ask for developer token and customer ID
            if service == "google_ads":
                console.print("\n[bold]Step 3: Google Ads Specific Credentials[/bold]")
                console.print("[dim]Find your Developer Token at: https://ads.google.com/aw/apicenter[/dim]")

                dev_token = Prompt.ask("Developer Token (optional, can add later)", default="").strip()
                customer_id = Prompt.ask("Customer ID (optional, can add later)", default="").strip()

                if dev_token:
                    credentials["developer_token"] = dev_token
                if customer_id:
                    credentials["customer_id"] = customer_id

            # For Google Analytics, ask for property ID
            elif service == "google_analytics":
                console.print("\n[bold]Step 3: Google Analytics Property ID[/bold]")
                property_id = Prompt.ask("GA4 Property ID (optional, can add later)", default="").strip()

                if property_id:
                    # Store in config, not secrets
                    config = {"property_id": property_id}
                    self.oauth_manager.save_oauth_credentials(service, credentials, config)
                    console.print(f"\n[green]✅ Google Analytics authentication complete![/green]")
                    return True

            # For Google Sheets, ask for spreadsheet ID
            elif service == "google_sheets":
                console.print("\n[bold]Step 3: Google Sheets Configuration[/bold]")
                console.print("[dim]You can add spreadsheet IDs later when adding the source[/dim]")

            # Save credentials
            self.oauth_manager.save_oauth_credentials(service, credentials)

            # Success message
            console.print(f"\n[green]✅ Google {service.replace('_', ' ').title()} authentication complete![/green]\n")
            console.print("[cyan]Next steps:[/cyan]")
            console.print(f"  1. Add {service.replace('_', ' ').title()} source: [bold]dango source add[/bold]")
            console.print(f"  2. Select '{service.replace('_', ' ').title()}' from the wizard")
            console.print("  3. Run sync to load data")

            return True

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            import traceback
            traceback.print_exc()
            return False

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


class FacebookOAuthProvider(BaseOAuthProvider):
    """
    Facebook/Meta Ads OAuth Provider

    Uses long-lived tokens (60-day expiry).
    For simplicity, we still use the manual token exchange method
    since Facebook OAuth for Marketing API is complex.
    """

    # Token exchange endpoint
    TOKEN_EXCHANGE_URL = "https://graph.facebook.com/v18.0/oauth/access_token"

    def authenticate(self) -> bool:
        """
        Run Facebook OAuth flow

        For MVP, we use the simpler approach of exchanging
        short-lived tokens for long-lived tokens.

        Returns:
            True if successful, False otherwise
        """
        try:
            console.print("\n[bold cyan]Facebook Ads Authentication[/bold cyan]\n")

            # Show instructions
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

            if Confirm.ask("\n[cyan]Open Facebook Graph API Explorer?[/cyan]", default=True):
                import webbrowser
                webbrowser.open("https://developers.facebook.com/tools/explorer/")

            # Get short-lived token
            console.print("\n[bold]Step 1: Short-lived Access Token[/bold]")
            short_token = Prompt.ask("Paste short-lived access token").strip()

            if not short_token:
                console.print("[red]✗ Access token is required[/red]")
                return False

            # Get App credentials
            console.print("\n[bold]Step 2: App Credentials[/bold]")
            console.print("[dim]Find at: https://developers.facebook.com/apps/[/dim]")

            app_id = Prompt.ask("Facebook App ID").strip()
            app_secret = Prompt.ask("Facebook App Secret", password=True).strip()

            if not app_id or not app_secret:
                console.print("[red]✗ App ID and Secret are required[/red]")
                return False

            # Exchange for long-lived token
            console.print("\n[cyan]Exchanging for long-lived token (60 days)...[/cyan]")
            long_token = self._exchange_token(short_token, app_id, app_secret)

            if not long_token:
                console.print("[red]✗ Token exchange failed[/red]")
                return False

            # Get Ad Account ID
            console.print("\n[bold]Step 3: Ad Account ID[/bold]")
            console.print("[dim]Find in Ads Manager URL: facebook.com/adsmanager/manage/accounts?act=ACCOUNT_ID[/dim]")
            account_id = Prompt.ask("Ad Account ID (e.g., act_123456789)").strip()

            # Save credentials
            credentials = {
                "access_token": long_token,
                "account_id": account_id
            }

            self.oauth_manager.save_oauth_credentials("facebook_ads", credentials)

            # Success message
            expiry_date = datetime.now() + timedelta(days=60)
            console.print(f"\n[green]✅ Facebook Ads authentication complete![/green]\n")
            console.print("[cyan]Next steps:[/cyan]")
            console.print("  1. Add Facebook Ads source: [bold]dango source add[/bold]")
            console.print("  2. Select 'Facebook Ads' from the wizard")
            console.print(f"  3. Token expires on: [yellow]{expiry_date.strftime('%Y-%m-%d')}[/yellow]")
            console.print("\n[yellow]⚠️  Set a reminder to re-authenticate before expiry[/yellow]")

            return True

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return False

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

    def authenticate(self) -> bool:
        """
        Run Shopify authentication (custom app method)

        Returns:
            True if successful, False otherwise
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
            shop_url = Prompt.ask("Shop URL (e.g., mystore.myshopify.com)").strip()

            # Normalize shop URL
            if not shop_url.endswith(".myshopify.com"):
                shop_url = f"{shop_url}.myshopify.com"

            # Get access token
            console.print("\n[bold]Step 2: Admin API Access Token[/bold]")
            access_token = Prompt.ask("Admin API access token", password=True).strip()

            if not shop_url or not access_token:
                console.print("[red]✗ Shop URL and access token are required[/red]")
                return False

            # Test the credentials
            console.print("\n[cyan]Testing connection...[/cyan]")
            if not self._test_connection(shop_url, access_token):
                console.print("[red]✗ Connection test failed[/red]")
                console.print("[yellow]Please verify your shop URL and access token[/yellow]")
                return False

            # Save credentials
            credentials = {
                "private_app_password": access_token,
                "shop_url": shop_url
            }

            self.oauth_manager.save_oauth_credentials("shopify", credentials)

            # Success message
            console.print(f"\n[green]✅ Shopify authentication complete![/green]\n")
            console.print("[cyan]Next steps:[/cyan]")
            console.print("  1. Add Shopify source: [bold]dango source add[/bold]")
            console.print("  2. Select 'Shopify' from the wizard")
            console.print("  3. Run sync to load data")

            return True

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return False

    def _test_connection(self, shop_url: str, access_token: str) -> bool:
        """
        Test Shopify connection

        Args:
            shop_url: Shop URL
            access_token: Admin API access token

        Returns:
            True if connection successful, False otherwise
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
            return True

        except requests.exceptions.RequestException as e:
            console.print(f"[red]✗ Connection test failed: {e}[/red]")
            return False
