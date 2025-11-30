"""
OAuth Authentication Manager

Handles browser-based OAuth flows for data sources.
Provides a simple interface for authenticating with various OAuth providers.

Supported providers:
- Google (Ads, Analytics, Sheets)
- Facebook/Meta (Ads)
- Shopify

Features:
- Local callback server for OAuth redirects
- CSRF protection with state parameters
- Automatic token storage in .dlt/secrets.toml
- Token refresh handling (where supported)
"""

import os
import secrets
import webbrowser
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode, parse_qs, urlparse
import http.server
import socketserver
import threading
import time

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

from dango.config.credentials import CredentialManager

console = Console()


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    """
    HTTP handler for OAuth callback

    Captures the authorization code from the OAuth provider's redirect.
    """

    # Class-level storage for OAuth response
    oauth_response = None
    oauth_error = None

    def do_GET(self):
        """Handle GET request from OAuth provider"""
        # Parse query parameters
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # Check for authorization code
        if 'code' in params:
            OAuthCallbackHandler.oauth_response = {
                'code': params['code'][0],
                'state': params.get('state', [None])[0]
            }

            # Send success page
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()

            success_html = """
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>OAuth Success</title>
            </head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: green;">&#10003; Authentication Successful!</h1>
                <p>You can close this window and return to the terminal.</p>
                <p style="color: #666; margin-top: 30px;">Dango is completing the setup...</p>
            </body>
            </html>
            """
            self.wfile.write(success_html.encode('utf-8'))

        # Check for error
        elif 'error' in params:
            OAuthCallbackHandler.oauth_error = {
                'error': params['error'][0],
                'error_description': params.get('error_description', ['Unknown error'])[0]
            }

            # Send error page
            self.send_response(400)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()

            error_html = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>OAuth Error</title>
            </head>
            <body style="font-family: Arial, sans-serif; text-align: center; padding: 50px;">
                <h1 style="color: red;">&#10007; Authentication Failed</h1>
                <p><strong>Error:</strong> {params['error'][0]}</p>
                <p>{params.get('error_description', [''])[0]}</p>
                <p style="color: #666; margin-top: 30px;">Please return to the terminal for instructions.</p>
            </body>
            </html>
            """
            self.wfile.write(error_html.encode('utf-8'))

        else:
            # Unknown request
            self.send_response(400)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write(b"Invalid OAuth callback")

    def log_message(self, format, *args):
        """Suppress server log messages"""
        pass


class OAuthManager:
    """
    Manages OAuth authentication flows for data sources

    Handles the complete OAuth flow:
    1. Generate authorization URL with state parameter (CSRF protection)
    2. Start local callback server
    3. Open browser for user authorization
    4. Capture authorization code from redirect
    5. Exchange code for access/refresh tokens
    6. Save tokens to .dlt/secrets.toml
    """

    def __init__(self, project_root: Path):
        """
        Initialize OAuth manager

        Args:
            project_root: Path to dango project root
        """
        self.project_root = Path(project_root)
        self.cred_manager = CredentialManager(project_root)

        # Load callback URL from environment or use default
        # This allows flexibility for:
        # - Local development with custom ports
        # - Cloud deployment with custom domains
        from dotenv import load_dotenv
        load_dotenv(self.project_root / ".env")

        self.callback_url = os.getenv(
            "DANGO_OAUTH_CALLBACK_URL",
            "http://localhost:8080/callback"
        )

        # Extract port from callback URL for local server
        # Format: http://localhost:8080/callback -> port 8080
        # Format: https://domain.com/oauth/callback -> port 443 (not used for local server)
        from urllib.parse import urlparse
        parsed = urlparse(self.callback_url)
        self.callback_port = parsed.port or (443 if parsed.scheme == "https" else 80)

        # Ensure .dlt directory exists
        self.cred_manager.init_dlt_directory()

    def start_oauth_flow(
        self,
        provider_name: str,
        auth_url: str,
        timeout_seconds: int = 300
    ) -> Optional[Dict[str, str]]:
        """
        Start OAuth flow and wait for callback

        Args:
            provider_name: Name of OAuth provider (for display)
            auth_url: Complete authorization URL (with all parameters)
            timeout_seconds: How long to wait for user authorization

        Returns:
            Dictionary with 'code' and 'state' if successful, None if failed
        """
        console.print(f"\n[bold cyan]{provider_name} OAuth Authorization[/bold cyan]\n")
        console.print("[dim]Starting local callback server...[/dim]")

        # Reset class-level response storage
        OAuthCallbackHandler.oauth_response = None
        OAuthCallbackHandler.oauth_error = None

        # Start callback server in a thread
        try:
            server = socketserver.TCPServer(("localhost", self.callback_port), OAuthCallbackHandler)
        except OSError as e:
            if "Address already in use" in str(e) or e.errno == 48:  # 48 = EADDRINUSE on macOS
                console.print(f"\n[red]✗ Port {self.callback_port} is already in use[/red]")
                console.print("\n[yellow]Possible solutions:[/yellow]")
                console.print(f"  1. Stop the process using port {self.callback_port}:")
                console.print(f"     [dim]lsof -i :{self.callback_port}[/dim]")
                console.print(f"     [dim]kill <PID>[/dim]")
                console.print(f"  2. Set a different callback port in .env:")
                console.print(f"     [dim]DANGO_OAUTH_CALLBACK_URL=http://localhost:8081/callback[/dim]")
                console.print(f"     [dim](Remember to update this in your OAuth app redirect URIs)[/dim]")
                return None
            else:
                raise

        server.timeout = timeout_seconds

        server_thread = threading.Thread(target=server.handle_request, daemon=True)
        server_thread.start()

        console.print(f"[dim]Callback server listening on port {self.callback_port}[/dim]")

        # Open browser for authorization
        console.print(f"\n[cyan]Opening browser for authorization...[/cyan]")
        console.print(f"[dim]If browser doesn't open, visit: {auth_url}[/dim]\n")

        webbrowser.open(auth_url)

        # Wait for callback with timeout
        console.print("[yellow]Waiting for authorization...[/yellow]")
        console.print("[dim](This window will close automatically once you authorize)[/dim]\n")

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if OAuthCallbackHandler.oauth_response is not None:
                console.print("[green]✓ Authorization received![/green]")
                # Don't call server.shutdown() - it can hang
                # The daemon thread will be cleaned up automatically
                server.server_close()
                return OAuthCallbackHandler.oauth_response

            if OAuthCallbackHandler.oauth_error is not None:
                error = OAuthCallbackHandler.oauth_error
                console.print(f"[red]✗ Authorization failed: {error['error']}[/red]")
                console.print(f"[red]{error['error_description']}[/red]")
                server.server_close()
                return None

            time.sleep(0.5)

        # Timeout
        console.print(f"[red]✗ Authorization timeout after {timeout_seconds} seconds[/red]")
        console.print("[yellow]Please try again and complete the authorization promptly.[/yellow]")
        server.server_close()
        return None

    def generate_state(self) -> str:
        """
        Generate a random state parameter for CSRF protection

        Returns:
            Random state string
        """
        return secrets.token_urlsafe(32)

    def save_oauth_credentials(
        self,
        source_name: str,
        credentials: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Save OAuth credentials to .dlt/secrets.toml

        Args:
            source_name: Name of the source (e.g., "google_ads", "facebook_ads")
            credentials: Dictionary of credentials to save
            config: Optional non-sensitive configuration to save to .dlt/config.toml
        """
        # Save credentials to secrets.toml
        secrets_data = {
            "sources": {
                source_name: credentials
            }
        }
        self.cred_manager.save_secrets(secrets_data, merge=True)

        # Save config if provided
        if config:
            config_data = {
                "sources": {
                    source_name: config
                }
            }
            self.cred_manager.save_config(config_data, merge=True)

        console.print(f"[green]✓ Credentials saved to .dlt/secrets.toml[/green]")

    def get_credentials(self, source_name: str) -> Optional[Dict[str, Any]]:
        """
        Get OAuth credentials for a source

        Args:
            source_name: Name of the source

        Returns:
            Dictionary of credentials or None if not found
        """
        return self.cred_manager.get_source_credentials(source_name)

    def has_credentials(self, source_name: str) -> bool:
        """
        Check if OAuth credentials exist for a source

        Args:
            source_name: Name of the source

        Returns:
            True if credentials exist, False otherwise
        """
        return self.cred_manager.has_credentials(source_name)

    def delete_credentials(self, source_name: str) -> bool:
        """
        Delete OAuth credentials for a source

        Args:
            source_name: Name of the source

        Returns:
            True if credentials were deleted, False if not found
        """
        return self.cred_manager.delete_source_credentials(source_name)


def create_oauth_manager(project_root: Path) -> OAuthManager:
    """
    Factory function to create an OAuth manager

    Args:
        project_root: Path to project root

    Returns:
        OAuthManager instance
    """
    return OAuthManager(project_root)
