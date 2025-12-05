"""
OAuth Authentication Helpers

Provides semi-automated OAuth flows for data sources that require OAuth.
Supports:
- Facebook Ads: Short-lived → Long-lived token exchange
- Google: OAuth 2.0 with refresh tokens

Tokens are stored in .env file for manual management.
"""

import os
import sys
import json
import webbrowser
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs, urlparse
import http.server
import socketserver
import threading

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

console = Console()


class OAuthHelper:
    """Base class for OAuth helpers"""

    def __init__(self, project_root: Path):
        """
        Initialize OAuth helper

        Args:
            project_root: Path to dango project root
        """
        self.project_root = project_root
        self.env_file = project_root / ".env"

    def save_to_env(self, key: str, value: str, comment: Optional[str] = None) -> None:
        """
        Save or update a value in .env file

        Args:
            key: Environment variable name
            value: Value to save
            comment: Optional comment to add above the variable
        """
        # Read existing .env if it exists
        env_lines = []
        if self.env_file.exists():
            with open(self.env_file, "r") as f:
                env_lines = f.readlines()

        # Remove existing key if present
        new_lines = []
        skip_next = False
        for i, line in enumerate(env_lines):
            if skip_next:
                skip_next = False
                continue
            if line.strip().startswith(f"{key}="):
                # Skip this line and any comment above it
                if i > 0 and env_lines[i - 1].strip().startswith("#"):
                    new_lines.pop()  # Remove comment line
                continue
            new_lines.append(line)

        # Add new key-value pair
        if comment:
            new_lines.append(f"# {comment}\n")
        new_lines.append(f"{key}={value}\n")

        # Write back to .env
        with open(self.env_file, "w") as f:
            f.writelines(new_lines)

        console.print(f"[green]✓[/green] Saved {key} to .env")

    def read_from_env(self, key: str) -> Optional[str]:
        """Read a value from .env file"""
        if not self.env_file.exists():
            return None

        with open(self.env_file, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1]

        return None


class FacebookOAuthHelper(OAuthHelper):
    """
    Facebook OAuth Helper

    Facebook token lifecycle:
    - Short-lived token: Expires in 1-2 hours
    - Long-lived token: Expires in 60 days
    - Marketing API token: Can be made never-expiring with Standard access

    For MVP: We use long-lived tokens (60 days), user re-authenticates manually.
    """

    def authenticate(self) -> bool:
        """
        Guide user through Facebook authentication

        Returns:
            True if authentication successful, False otherwise
        """
        try:
            console.print("\n[bold cyan]Facebook Ads Authentication[/bold cyan]\n")

            # Show instructions
            instructions = [
                "1. Go to: https://developers.facebook.com/tools/explorer/",
                "2. Select your app (or create one)",
                "3. Click 'Generate Access Token'",
                "4. Grant permissions: ads_read, ads_management",
                "5. Copy the access token (short-lived, 1-2 hours)",
                "",
                "[yellow]Note: We'll exchange this for a long-lived token (60 days)[/yellow]",
            ]

            console.print(Panel("\n".join(instructions), title="Setup Instructions"))

            # Ask if user wants to open browser
            if Confirm.ask("\n[cyan]Open Facebook Graph API Explorer in browser?[/cyan]"):
                webbrowser.open("https://developers.facebook.com/tools/explorer/")
                console.print("[dim]Browser opened[/dim]\n")

            # Get short-lived token from user
            console.print("[bold]Step 1: Get short-lived token[/bold]")
            short_token = Prompt.ask("Paste short-lived access token")

            if not short_token:
                console.print("[red]✗ No token provided[/red]")
                return False

            # Get App ID and App Secret for token exchange
            console.print("\n[bold]Step 2: Get App credentials[/bold]")
            console.print("[dim]Find at: https://developers.facebook.com/apps/[/dim]")

            app_id = Prompt.ask("Facebook App ID")
            app_secret = Prompt.ask("Facebook App Secret", password=True)

            if not app_id or not app_secret:
                console.print("[red]✗ App credentials required[/red]")
                return False

            # Exchange for long-lived token
            console.print("\n[cyan]Exchanging for long-lived token...[/cyan]")
            long_token = self._exchange_token(short_token, app_id, app_secret)

            if not long_token:
                console.print("[red]✗ Token exchange failed[/red]")
                return False

            # Save to .env
            expiry_date = datetime.now() + timedelta(days=60)
            self.save_to_env(
                "FB_ACCESS_TOKEN",
                long_token,
                f"Facebook long-lived token (expires ~{expiry_date.strftime('%Y-%m-%d')})",
            )

            # Get Ad Account ID
            console.print("\n[bold]Step 3: Get Ad Account ID[/bold]")
            console.print("[dim]Find in Ads Manager URL: facebook.com/adsmanager/manage/accounts?act=ACCOUNT_ID[/dim]")
            account_id = Prompt.ask("Ad Account ID (e.g., act_123456789)")

            if account_id:
                self.save_to_env("FB_AD_ACCOUNT_ID", account_id, "Facebook Ads Account ID")

            # Success message
            console.print("\n[green]✅ Facebook authentication complete![/green]\n")
            console.print("[cyan]Next steps:[/cyan]")
            console.print("  1. Add Facebook Ads source: dango source add")
            console.print("  2. Select 'Facebook Ads' from wizard")
            console.print(f"  3. Token expires in ~60 days ({expiry_date.strftime('%Y-%m-%d')})")
            console.print("\n[yellow]⚠️  Set a reminder to re-authenticate before expiry[/yellow]")

            return True

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return False

    def _exchange_token(
        self, short_token: str, app_id: str, app_secret: str
    ) -> Optional[str]:
        """
        Exchange short-lived token for long-lived token

        Args:
            short_token: Short-lived access token (1-2 hours)
            app_id: Facebook App ID
            app_secret: Facebook App Secret

        Returns:
            Long-lived access token (60 days) or None if exchange fails
        """
        try:
            import requests

            # Facebook Graph API token exchange endpoint
            url = "https://graph.facebook.com/v18.0/oauth/access_token"
            params = {
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token,
            }

            response = requests.get(url, params=params)
            response.raise_for_status()

            data = response.json()
            long_token = data.get("access_token")

            if long_token:
                console.print("[green]✓[/green] Long-lived token obtained")
                return long_token
            else:
                console.print("[red]✗ No access_token in response[/red]")
                return None

        except ImportError:
            console.print("[red]✗ 'requests' library not installed[/red]")
            console.print("Install with: pip install requests")
            return None
        except Exception as e:
            console.print(f"[red]✗ Token exchange error: {e}[/red]")
            return None


class GoogleOAuthHelper(OAuthHelper):
    """
    Google OAuth Helper

    Google OAuth uses refresh tokens that don't expire.
    Once obtained, dlt handles token refresh automatically.

    Supports:
    - Google Sheets
    - Google Analytics
    - Google Ads
    """

    # OAuth 2.0 scopes for different Google services
    SCOPES = {
        "sheets": [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
        "analytics": ["https://www.googleapis.com/auth/analytics.readonly"],
        "ads": ["https://www.googleapis.com/auth/adwords"],
    }

    def authenticate(self, service: str = "sheets") -> bool:
        """
        Guide user through Google OAuth flow

        Args:
            service: Google service to authenticate (sheets, analytics, ads)

        Returns:
            True if authentication successful, False otherwise
        """
        try:
            console.print(f"\n[bold cyan]Google {service.title()} Authentication[/bold cyan]\n")

            # Show instructions
            instructions = [
                "Google OAuth requires OAuth 2.0 credentials from Google Cloud Console.",
                "",
                "[bold]Setup steps:[/bold]",
                "1. Go to: https://console.cloud.google.com/",
                "2. Create a new project (or select existing)",
                "3. Enable the required API:",
                f"   - Google Sheets API (for sheets)",
                f"   - Google Analytics Data API (for analytics)",
                f"   - Google Ads API (for ads)",
                "4. Go to 'Credentials' > 'Create Credentials' > 'OAuth client ID'",
                "5. Application type: 'Desktop app'",
                "6. Download the credentials JSON file",
                "",
                "[yellow]For this MVP: We'll use a simpler flow with service accounts[/yellow]",
                "[yellow]Or you can use dlt's built-in OAuth (see dlt docs)[/yellow]",
            ]

            console.print(Panel("\n".join(instructions), title="Setup Instructions"))

            # For MVP: Guide user to use service account (simpler)
            console.print("\n[bold]Recommended approach for MVP:[/bold]")
            console.print("1. Use Service Account instead of OAuth")
            console.print("2. Create Service Account in Google Cloud Console")
            console.print("3. Download JSON key file")
            console.print("4. Share Google Sheets/Analytics with service account email\n")

            if Confirm.ask("[cyan]Open Google Cloud Console?[/cyan]"):
                webbrowser.open("https://console.cloud.google.com/apis/credentials")
                console.print("[dim]Browser opened[/dim]\n")

            # Get credentials path
            console.print("[bold]Enter path to credentials JSON file:[/bold]")
            creds_path = Prompt.ask("Credentials file path")

            if not creds_path:
                console.print("[red]✗ No credentials file provided[/red]")
                return False

            # Validate JSON file
            creds_path = Path(creds_path).expanduser()
            if not creds_path.exists():
                console.print(f"[red]✗ File not found: {creds_path}[/red]")
                return False

            # Read and validate JSON
            try:
                with open(creds_path, "r") as f:
                    creds_data = json.load(f)

                # Check if it's service account or OAuth client
                if "type" in creds_data and creds_data["type"] == "service_account":
                    console.print("[green]✓[/green] Service account credentials detected")
                    account_email = creds_data.get("client_email", "unknown")
                    console.print(f"[dim]Service account: {account_email}[/dim]")
                elif "installed" in creds_data or "web" in creds_data:
                    console.print("[green]✓[/green] OAuth client credentials detected")
                else:
                    console.print("[yellow]⚠[/yellow] Unknown credentials format")

            except json.JSONDecodeError:
                console.print("[red]✗ Invalid JSON file[/red]")
                return False

            # Save credentials to .env as JSON string
            creds_json = json.dumps(creds_data)
            self.save_to_env(
                "GOOGLE_CREDENTIALS",
                creds_json,
                "Google credentials (service account or OAuth)",
            )

            # Additional info for Google Ads
            if service == "ads":
                console.print("\n[bold]Google Ads requires additional credentials:[/bold]")
                dev_token = Prompt.ask("Developer Token (from Google Ads API Center)")
                if dev_token:
                    self.save_to_env("GOOGLE_ADS_DEV_TOKEN", dev_token, "Google Ads Developer Token")

                customer_id = Prompt.ask("Customer ID (optional, can add later)", default="")
                if customer_id:
                    self.save_to_env("GOOGLE_ADS_CUSTOMER_ID", customer_id, "Google Ads Customer ID")

            # Success message
            console.print(f"\n[green]✅ Google {service.title()} authentication complete![/green]\n")
            console.print("[cyan]Next steps:[/cyan]")
            console.print(f"  1. Add Google {service.title()} source: dango source add")
            console.print(f"  2. Select 'Google {service.title()}' from wizard")

            if service == "sheets":
                console.print("  3. Share spreadsheets with service account email")
            elif service == "analytics":
                console.print("  3. Add service account as viewer in GA4 property")

            return True

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return False


def authenticate_facebook(project_root: Path) -> bool:
    """Authenticate with Facebook Ads"""
    helper = FacebookOAuthHelper(project_root)
    return helper.authenticate()


def authenticate_google(project_root: Path, service: str = "sheets") -> bool:
    """Authenticate with Google services"""
    helper = GoogleOAuthHelper(project_root)
    return helper.authenticate(service)


def check_token_expiry(project_root: Path, source_type: str) -> Optional[str]:
    """
    Check if tokens for a source are expired or expiring soon

    Args:
        project_root: Path to project root
        source_type: Type of source (e.g., "facebook_ads", "google_sheets")

    Returns:
        Warning message if token is expired/expiring, None if OK
    """
    env_file = project_root / ".env"
    if not env_file.exists():
        return None

    # Read .env file
    env_vars = {}
    with open(env_file, "r") as f:
        for line in f:
            line = line.strip()
            # Check for expiry comments
            if line.startswith("# Facebook long-lived token (expires"):
                # Extract expiry date
                try:
                    parts = line.split("expires ~")
                    if len(parts) > 1:
                        expiry_str = parts[1].rstrip(")")
                        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d")
                        days_remaining = (expiry_date - datetime.now()).days

                        if days_remaining < 0:
                            return f"[red]Facebook token expired {abs(days_remaining)} days ago[/red]\nRun: dango auth facebook_ads"
                        elif days_remaining < 7:
                            return f"[yellow]Facebook token expires in {days_remaining} days[/yellow]\nConsider running: dango auth facebook_ads"
                except Exception:
                    pass

    return None
