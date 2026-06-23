"""dango/oauth/providers.py

Provider-specific OAuth flows for data sources.
"""

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from dango.oauth import OAuthManager
from dango.oauth.storage import OAuthCredential, OAuthStorage

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

    def authenticate(self, source_name: str | None = None) -> str | None:
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

    # Exact API names as shown in Google Cloud Console
    API_NAMES = {
        "google_ads": "Google Ads API",
        "google_analytics": "Google Analytics Data API",
        "google_sheets": "Google Sheets API",
    }

    # Scopes for different Google services
    # Always include userinfo.email to identify the authenticated user
    BASE_SCOPES = [
        "https://www.googleapis.com/auth/userinfo.email",
    ]

    SCOPES = {
        "google_ads": ["https://www.googleapis.com/auth/adwords"],
        "google_analytics": ["https://www.googleapis.com/auth/analytics.readonly"],
        "google_sheets": [
            "https://www.googleapis.com/auth/spreadsheets.readonly"
            # Note: Drive scope removed - we only read specific spreadsheets via Sheets API,
            # don't need access to all Drive files
        ],
    }

    def authenticate(
        self, service: str = "google_ads", source_name: str | None = None
    ) -> str | None:
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
            console.print(
                f"\n[bold cyan]Google {service.replace('_', ' ').title()} Authentication[/bold cyan]\n"
            )

            # Try to load credentials from .env first
            env_file = self.project_root / ".env"
            load_dotenv(env_file, override=True)
            client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
            client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()

            # Debug: check if .env exists and has the keys
            if not client_id and env_file.exists():
                # Try reading directly from file as fallback
                with open(env_file) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("GOOGLE_CLIENT_ID="):
                            client_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("GOOGLE_CLIENT_SECRET="):
                            client_secret = line.split("=", 1)[1].strip().strip('"').strip("'")

            while True:
                if client_id and client_secret:
                    # Credentials found in .env
                    console.print("[green]✓ Found OAuth credentials in .env[/green]")
                    console.print(f"[dim]  Client ID: {client_id[:20]}...{client_id[-10:]}[/dim]\n")
                    # Check if actual Google tokens exist - without tokens, .env creds
                    # may be stale from a previous failed OAuth flow (P2-3 guard).
                    has_any_google_token = any(
                        self.oauth_storage.exists(svc)
                        for svc in ["google_ads", "google_analytics", "google_sheets"]
                    )
                    if not has_any_google_token:
                        console.print(
                            "\n[yellow]⚠️  OAuth credentials found in .env but no Google tokens exist.[/yellow]"
                        )
                        console.print(
                            "   [yellow]This can happen if a previous OAuth flow failed to complete.[/yellow]"
                        )
                        console.print(
                            "   [yellow]Check your credentials at: https://console.cloud.google.com/apis/credentials[/yellow]"
                        )
                        if Confirm.ask("\n[cyan]Re-enter credentials?[/cyan]", default=False):
                            from dotenv import unset_key

                            client_id = ""
                            client_secret = ""
                            os.environ.pop("GOOGLE_CLIENT_ID", None)
                            os.environ.pop("GOOGLE_CLIENT_SECRET", None)
                            if env_file.exists():
                                unset_key(str(env_file), "GOOGLE_CLIENT_ID")
                                unset_key(str(env_file), "GOOGLE_CLIENT_SECRET")
                            continue
                else:
                    # Show setup instructions only if credentials not found
                    api_name = self.API_NAMES.get(service, "the required API")
                    instructions = [
                        "[bold]Prerequisites:[/bold]",
                        "1. Create a Google Cloud Project at https://console.cloud.google.com/",
                        "",
                        "2. Configure the OAuth consent screen:",
                        "   • Go to APIs & Services > OAuth consent screen",
                        "   • Select [yellow]External[/yellow] user type > Create",
                        "   • Fill in App name, User support email, Developer email",
                        "   • Click through Scopes (no changes needed)",
                        "   • Add your Google account email as a Test User",
                        "   • Save and return to dashboard",
                        "",
                        f"3. Enable the [bold]{api_name}[/bold]:",
                        "   • Go to APIs & Services > Library",
                        f'   • Search for "{api_name}" and click Enable',
                        "",
                        "4. Create OAuth 2.0 credentials:",
                        "   • Go to APIs & Services > Credentials",
                        "   • Create Credentials > OAuth client ID",
                        "   • Application type: [yellow]Web application[/yellow]",
                        f"   • Add Authorized redirect URI: {self.oauth_manager.callback_url}",
                        "   • Copy the Client ID and Client Secret",
                        "",
                        "[yellow]⚠  Testing mode:[/yellow] Tokens expire after 7 days.",
                        "   To get permanent tokens, publish your app:",
                        "   OAuth consent screen > Publishing status > [bold]Publish App[/bold]",
                        "",
                        "[dim]Tip: Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to .env to skip this step[/dim]",
                    ]

                    console.print(
                        Panel(
                            "\n".join(instructions), title="Setup Instructions", border_style="cyan"
                        )
                    )

                    # Get OAuth client credentials
                    console.print("\n[bold]Enter OAuth Client Credentials:[/bold]")
                    client_id = _clean_pasted_input(Prompt.ask("Client ID"))
                    client_secret = _clean_pasted_input(Prompt.ask("Client Secret", password=True))

                if not client_id or not client_secret:
                    console.print("[red]✗ Client ID and Secret are required[/red]")
                    return None

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
                console.print(
                    "\n[yellow]If your app is in Testing mode (the default):[/yellow]"
                    "\n[yellow]  Your browser will show 'Google hasn't verified this app'[/yellow]"
                    "\n[yellow]  Click [bold]Advanced[/bold] → [bold]Go to <app name> "
                    "(unsafe)[/bold] → [bold]Continue[/bold][/yellow]"
                )
                console.print(
                    "[yellow]If your app is Published:[/yellow]"
                    "\n[yellow]  You'll see a standard Google consent screen — "
                    "click [bold]Allow[/bold][/yellow]"
                )
                oauth_response = self.oauth_manager.start_oauth_flow("Google", auth_url)

                if not oauth_response:
                    console.print("[red]✗ OAuth flow failed or timed out[/red]")
                    return None

                # Handle re-enter credentials sentinel from start_oauth_flow().
                # Clear cached credentials and loop back to the credential prompt
                # so the user can re-enter immediately without a second prompt.
                if "action" in oauth_response:
                    from dotenv import unset_key

                    os.environ.pop("GOOGLE_CLIENT_ID", None)
                    os.environ.pop("GOOGLE_CLIENT_SECRET", None)
                    if env_file.exists():
                        unset_key(str(env_file), "GOOGLE_CLIENT_ID")
                        unset_key(str(env_file), "GOOGLE_CLIENT_SECRET")
                    console.print(
                        "\n[yellow]Cleared cached credentials. Please enter new OAuth credentials.[/yellow]\n"
                    )
                    client_id = ""
                    client_secret = ""
                    continue

                # Exit loop on successful OAuth callback (has code, not sentinel)
                break

            # Verify state parameter
            if oauth_response.get("state") != state:
                console.print("[red]✗ Invalid state parameter (possible CSRF attack)[/red]")
                return None

            # Exchange authorization code for tokens
            console.print("\n[cyan]Exchanging authorization code for tokens...[/cyan]")
            tokens = self._exchange_code_for_tokens(
                code=oauth_response["code"],
                client_id=client_id,
                client_secret=client_secret,
            )

            if not tokens:
                console.print("[red]✗ Token exchange failed[/red]")
                return None

            # Save OAuth client credentials to .env only after token exchange succeeds.
            # Token exchange validates the client_id/secret against Google's endpoint.
            # If invalid, we never reach this point and bad credentials are not persisted.
            if client_id and client_secret:
                from dotenv import set_key

                if not env_file.exists():
                    env_file.touch()
                set_key(str(env_file), "GOOGLE_CLIENT_ID", client_id)
                set_key(str(env_file), "GOOGLE_CLIENT_SECRET", client_secret)
                console.print("[dim]Saved credentials to .env for future use[/dim]")

            # Fetch user info to get email (identifier)
            console.print("\n[cyan]Fetching user info...[/cyan]")
            user_info = self._fetch_user_info(tokens["access_token"])

            if not user_info or "email" not in user_info:
                console.print("[red]✗ Could not get user email[/red]")
                return None

            email = user_info["email"]

            # Save credentials in dlt format
            # Include impersonated_email for Google Ads (required by dlt function signature)
            # project_id is required by dlt's GcpOAuthCredentials - use placeholder
            credentials = {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": tokens["refresh_token"],
                "project_id": "dango-oauth",  # Required by dlt GcpOAuthCredentials
                "impersonated_email": email,  # Used by Google Ads
            }

            # For Google Ads, also ask for developer token and customer ID
            if service == "google_ads":
                console.print("\n[bold]Step 3: Google Ads Specific Credentials[/bold]")
                console.print("[dim]Find your Developer Token in Google Ads:[/dim]")
                console.print("[dim]  • Sign in at https://ads.google.com[/dim]")
                console.print("[dim]  • Tools & Settings > Setup > API Center[/dim]")
                console.print("[dim]  • Note: Requires a Manager Account (MCC)[/dim]")

                # Collect and confirm Google Ads credentials
                while True:
                    dev_token = _clean_pasted_input(
                        Prompt.ask(
                            "Developer Token (required for sync, press Enter to skip for now)",
                            default="",
                        )
                    )
                    customer_id = _clean_pasted_input(
                        Prompt.ask(
                            "Customer ID (digits only, no dashes, e.g., 1234567890)", default=""
                        )
                    )
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
            name = user_info.get("name")
            account_info = f"{name} ({email})" if name else email
            oauth_cred = OAuthCredential(
                source_type=service,  # e.g., "google_ads", "google_sheets"
                provider="google",
                identifier=email,
                account_info=account_info,
                credentials=credentials,
                created_at=datetime.now(),
                expires_at=None,  # Google refresh tokens don't expire
                metadata={"scopes": all_scopes},
            )

            # Save using new storage - writes to sources.{service}.credentials.*
            if not self.oauth_storage.save(oauth_cred):
                return None

            # Success message
            console.print("\n[green]✅ Google authentication complete![/green]")
            console.print(f"[dim]Credentials saved for {service}[/dim]")

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
        self, code: str, client_id: str, client_secret: str
    ) -> dict[str, str] | None:
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

            if "refresh_token" not in tokens:
                console.print("[yellow]⚠️  No refresh token received.[/yellow]")
                console.print(
                    "[yellow]   This usually means you've authorized this app before.[/yellow]"
                )
                console.print("[yellow]   To get a refresh token, revoke access at:[/yellow]")
                console.print("[yellow]   https://myaccount.google.com/permissions[/yellow]")
                console.print("[yellow]   Then try again.[/yellow]")
                return None

            console.print("[green]✓ Tokens received successfully![/green]")
            return tokens

        except requests.exceptions.RequestException as e:
            console.print(f"[red]✗ Token exchange failed: {e}[/red]")
            if hasattr(e.response, "text"):
                console.print(f"[red]Response: {e.response.text}[/red]")
            return None

    def _fetch_user_info(self, access_token: str) -> dict[str, Any] | None:
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

    def authenticate(self, source_name: str | None = None) -> str | None:
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
                    expired_date = (
                        existing_cred.expires_at.strftime("%Y-%m-%d")
                        if existing_cred.expires_at
                        else "unknown"
                    )
                    console.print(f"[red]⚠️  Your Facebook token has expired ({expired_date})[/red]")
                    console.print(
                        f"[dim]Account: {existing_cred.account_info or existing_cred.identifier}[/dim]"
                    )
                    console.print("[cyan]Let's get you a new token...[/cyan]\n")
                else:
                    # Token is still valid - check for auto-extend capability
                    app_id = (
                        existing_cred.metadata.get("app_id") if existing_cred.metadata else None
                    )
                    app_secret = (
                        existing_cred.metadata.get("app_secret") if existing_cred.metadata else None
                    )
                    current_token = (
                        existing_cred.credentials.get("access_token")
                        if existing_cred.credentials
                        else None
                    )

                    if app_id and app_secret and current_token:
                        days_left = existing_cred.days_until_expiry()
                        console.print(
                            f"[cyan]Found existing valid token (expires in {days_left} days)[/cyan]"
                        )
                        console.print(
                            f"[dim]Account: {existing_cred.account_info or existing_cred.identifier}[/dim]\n"
                        )

                        if Confirm.ask(
                            "[cyan]Extend token for another 60 days?[/cyan]", default=True
                        ):
                            # Attempt auto-extend
                            console.print("\n[cyan]Exchanging token for new 60-day token...[/cyan]")
                            new_token = self._exchange_token(current_token, app_id, app_secret)

                            if new_token:
                                # Update credentials with new token and expiry
                                existing_cred.credentials["access_token"] = new_token
                                existing_cred.expires_at = datetime.now() + timedelta(days=60)
                                existing_cred.created_at = datetime.now()

                                if self.oauth_storage.save(existing_cred):
                                    console.print(
                                        "\n[green]✅ Token extended successfully![/green]"
                                    )
                                    console.print(
                                        f"[yellow]New expiry:[/yellow] {existing_cred.expires_at.strftime('%Y-%m-%d')} (60 days)"
                                    )
                                    return "facebook_ads"
                                else:
                                    console.print("[red]✗ Failed to save extended token[/red]")
                            else:
                                console.print(
                                    "[yellow]Auto-extend failed (token may have been invalidated).[/yellow]"
                                )
                                console.print(
                                    "[dim]Falling back to manual re-authentication...[/dim]\n"
                                )
                        else:
                            console.print("[dim]Proceeding with full re-authentication...[/dim]\n")
                    elif app_id and current_token and not app_secret:
                        # Legacy credentials without app_secret - inform user
                        console.print(
                            "[yellow]Found existing token but missing app credentials for auto-extend.[/yellow]"
                        )
                        console.print(
                            "[dim]After this re-authentication, future extends will be automatic.[/dim]\n"
                        )

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

            console.print(
                Panel("\n".join(instructions), title="Setup Instructions", border_style="cyan")
            )

            # Get short-lived token
            console.print("\n[bold]Step 1: Short-lived Access Token[/bold]")
            short_token = _clean_pasted_input(Prompt.ask("Paste short-lived access token"))

            if not short_token:
                console.print("[red]✗ Access token is required[/red]")
                return None

            console.print(f"[dim]  Captured: {short_token[:15]}...{short_token[-8:]}[/dim]")

            # Get App credentials
            console.print("\n[bold]Step 2: App Credentials[/bold]")
            console.print(
                "[dim]Find at: developers.facebook.com/apps → Your App → Settings → Basic[/dim]"
            )

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
            console.print(
                "[dim]Go to adsmanager.facebook.com → Look at URL for act=XXXXX or click account dropdown[/dim]"
            )

            while True:
                account_id = _clean_pasted_input(Prompt.ask("Ad Account ID (e.g., 123456789)"))

                if not account_id:
                    console.print("[red]✗ Account ID is required[/red]")
                    continue

                # Normalize account_id (remove "act_" prefix if present for consistency)
                account_id_clean = account_id.replace("act_", "")

                console.print("\n[bold]Please verify:[/bold]")
                console.print(f"  Account ID: {account_id_clean}")

                if Confirm.ask("\n[cyan]Is this correct?[/cyan]", default=True):
                    break

                console.print("\n[yellow]Let's re-enter:[/yellow]")

            # Save credentials - store clean account_id without "act_" prefix
            credentials = {
                "access_token": long_token,
                "account_id": account_id_clean,  # Clean ID - helpers.py will add "act_" prefix
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
                    "app_secret": app_secret,  # Stored for token auto-extend
                },
            )

            # Save using new storage - writes to sources.facebook_ads.credentials.*
            if not self.oauth_storage.save(oauth_cred):
                return None

            # Success message
            console.print("\n[green]✅ Facebook Ads authentication complete![/green]")
            console.print("[dim]Credentials saved for facebook_ads[/dim]")
            console.print(
                f"[yellow]Token expires:[/yellow] {expires_at.strftime('%Y-%m-%d')} (60 days)"
            )
            console.print("[yellow]⚠️  Set a reminder to re-authenticate before expiry[/yellow]")

            return "facebook_ads"  # Return source_type

        except KeyboardInterrupt:
            console.print("\n[yellow]Authentication cancelled[/yellow]")
            return None
        except Exception as e:
            console.print(f"\n[red]✗ Error: {e}[/red]")
            return None

    def _exchange_token(self, short_token: str, app_id: str, app_secret: str) -> str | None:
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
            if hasattr(e.response, "text"):
                console.print(f"[red]Response: {e.response.text}[/red]")
            return None
