"""
OAuth Credential Storage

Stores OAuth credentials in dlt's expected format:
- Credentials at sources.{source_type}.credentials.* (dlt reads this)
- Metadata at dango.oauth.{source_type}.* (for tracking expiry, provider info)

This follows dlt best practice - credentials are stored where dlt expects them,
no injection or translation needed at sync time.
"""

import re
import toml
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from rich.console import Console

console = Console()


@dataclass
class OAuthCredential:
    """
    OAuth credential with metadata

    Attributes:
        source_type: dlt source type (e.g., "google_ads", "google_sheets")
        provider: Provider type (e.g., "google", "facebook_ads", "shopify")
        identifier: Provider-specific identifier (email, account_id, shop_url)
        account_info: Human-readable account description
        credentials: Token data (client_id, client_secret, refresh_token, etc.)
        created_at: When credential was created
        expires_at: When credential expires (None for non-expiring)
        last_refreshed: Last time token was refreshed
        metadata: Additional provider-specific metadata
    """
    source_type: str
    provider: str
    identifier: str
    account_info: str
    credentials: Dict[str, Any]
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_refreshed: Optional[datetime] = None
    metadata: Optional[Dict[str, Any]] = None

    def is_expired(self) -> bool:
        """Check if credential has expired"""
        if not self.expires_at:
            return False
        return datetime.now() >= self.expires_at

    def days_until_expiry(self) -> Optional[int]:
        """Get days until expiry, or None if no expiry"""
        if not self.expires_at:
            return None
        delta = self.expires_at - datetime.now()
        return max(0, delta.days)

    def is_expiring_soon(self, days: int = 7) -> bool:
        """Check if credential expires within N days"""
        days_left = self.days_until_expiry()
        return days_left is not None and days_left <= days


class OAuthStorage:
    """
    OAuth credential storage manager

    Stores credentials in dlt's expected format at sources.{source_type}.credentials.*
    Stores metadata at dango.oauth.{source_type}.* for tracking.
    """

    def __init__(self, project_root: Path):
        """
        Initialize OAuth storage

        Args:
            project_root: Project root directory
        """
        self.project_root = Path(project_root)
        self.dlt_dir = self.project_root / ".dlt"
        self.secrets_file = self.dlt_dir / "secrets.toml"

        # Ensure .dlt directory exists
        self.dlt_dir.mkdir(parents=True, exist_ok=True)

        # Create secrets.toml if it doesn't exist
        if not self.secrets_file.exists():
            self.secrets_file.write_text("")

    def _load_secrets(self) -> Dict[str, Any]:
        """Load secrets.toml"""
        if self.secrets_file.exists() and self.secrets_file.stat().st_size > 0:
            return toml.load(self.secrets_file)
        return {}

    def _save_secrets(self, secrets: Dict[str, Any]) -> None:
        """Save secrets.toml"""
        with open(self.secrets_file, 'w') as f:
            toml.dump(secrets, f)

    def save(self, oauth_cred: OAuthCredential) -> bool:
        """
        Save OAuth credential in dlt's expected format

        Writes to:
        - sources.{source_type}.credentials.* (dlt reads this)
        - dango.oauth.{source_type}.* (metadata for tracking)

        Args:
            oauth_cred: OAuth credential to save

        Returns:
            True if successful, False otherwise
        """
        try:
            secrets = self._load_secrets()

            # Ensure paths exist
            if 'sources' not in secrets:
                secrets['sources'] = {}
            if oauth_cred.source_type not in secrets['sources']:
                secrets['sources'][oauth_cred.source_type] = {}
            if 'dango' not in secrets:
                secrets['dango'] = {}
            if 'oauth' not in secrets['dango']:
                secrets['dango']['oauth'] = {}

            # Write credentials in dlt's expected format
            #
            # dlt sources have two credential patterns:
            # 1. Google sources use GcpOAuthCredentials - expects nested 'credentials' object
            # 2. All other sources use flat parameters (dlt.secrets.value for individual params)
            #
            # This generalized approach future-proofs all OAuth sources without per-source hardcoding.
            source_section = secrets['sources'][oauth_cred.source_type]
            creds = oauth_cred.credentials

            # Only Google sources use credentials object (GcpOAuthCredentials pattern)
            CREDENTIALS_OBJECT_SOURCES = {'google_ads', 'google_analytics', 'google_sheets'}

            if oauth_cred.source_type in CREDENTIALS_OBJECT_SOURCES:
                # Google: nested credentials object (dlt GcpOAuthCredentials)
                source_section['credentials'] = {
                    'client_id': creds.get('client_id'),
                    'client_secret': creds.get('client_secret'),
                    'refresh_token': creds.get('refresh_token'),
                    'project_id': creds.get('project_id', 'dango-oauth'),
                }
                # Google Ads specific sibling fields (per dlt docs)
                if oauth_cred.source_type == 'google_ads':
                    if creds.get('impersonated_email'):
                        source_section['impersonated_email'] = creds['impersonated_email']
                    if creds.get('dev_token'):
                        source_section['dev_token'] = creds['dev_token']
                    if creds.get('customer_id'):
                        source_section['customer_id'] = creds['customer_id']
            else:
                # All other sources: flat parameters (dlt.secrets.value pattern)
                # This covers Facebook, Shopify, Slack, HubSpot, Notion, etc.
                for key, value in creds.items():
                    if value is not None:
                        source_section[key] = value

            # Write metadata for tracking (not used by dlt)
            secrets['dango']['oauth'][oauth_cred.source_type] = {
                'provider': oauth_cred.provider,
                'identifier': oauth_cred.identifier,
                'account_info': oauth_cred.account_info,
                'created_at': oauth_cred.created_at.isoformat(),
                'expires_at': oauth_cred.expires_at.isoformat() if oauth_cred.expires_at else None,
                'last_refreshed': oauth_cred.last_refreshed.isoformat() if oauth_cred.last_refreshed else None,
                'metadata': oauth_cred.metadata,
            }

            self._save_secrets(secrets)
            console.print(f"[green]✓ Saved OAuth credentials for {oauth_cred.source_type}[/green]")
            return True

        except Exception as e:
            console.print(f"[red]✗ Failed to save OAuth credential: {e}[/red]")
            return False

    def get(self, source_type: str) -> Optional[OAuthCredential]:
        """
        Get OAuth credential for a source type

        Args:
            source_type: dlt source type (e.g., "google_ads")

        Returns:
            OAuthCredential if found, None otherwise
        """
        try:
            secrets = self._load_secrets()

            # Get source section
            source_section = secrets.get('sources', {}).get(source_type, {})
            if not source_section:
                return None

            # Google sources use nested credentials object (GcpOAuthCredentials)
            # All other sources use flat parameters
            CREDENTIALS_OBJECT_SOURCES = {'google_ads', 'google_analytics', 'google_sheets'}

            if source_type in CREDENTIALS_OBJECT_SOURCES:
                # Google: look for nested 'credentials' object
                creds = source_section.get('credentials')
                if not creds:
                    return None
            else:
                # Non-Google: flat parameters are the credentials
                # Check for common OAuth credential keys
                if 'access_token' in source_section or 'api_key' in source_section:
                    creds = source_section
                else:
                    return None

            # Get metadata if available
            meta = secrets.get('dango', {}).get('oauth', {}).get(source_type, {})

            return OAuthCredential(
                source_type=source_type,
                provider=meta.get('provider', 'unknown'),
                identifier=meta.get('identifier', ''),
                account_info=meta.get('account_info', ''),
                credentials=creds,
                created_at=datetime.fromisoformat(meta['created_at']) if meta.get('created_at') else datetime.now(),
                expires_at=datetime.fromisoformat(meta['expires_at']) if meta.get('expires_at') else None,
                last_refreshed=datetime.fromisoformat(meta['last_refreshed']) if meta.get('last_refreshed') else None,
                metadata=meta.get('metadata'),
            )

        except Exception as e:
            console.print(f"[red]✗ Failed to load OAuth credential for {source_type}: {e}[/red]")
            return None

    def list(self, provider: Optional[str] = None) -> List[OAuthCredential]:
        """
        List all OAuth credentials, optionally filtered by provider

        Args:
            provider: Filter by provider type (optional)

        Returns:
            List of OAuth credentials
        """
        try:
            secrets = self._load_secrets()
            credentials = []

            # Check each source type for credentials
            oauth_meta = secrets.get('dango', {}).get('oauth', {})

            # Google sources use nested credentials object (GcpOAuthCredentials)
            CREDENTIALS_OBJECT_SOURCES = {'google_ads', 'google_analytics', 'google_sheets'}

            for source_type, meta in oauth_meta.items():
                # Filter by provider if specified
                if provider and meta.get('provider') != provider:
                    continue

                # Get source section
                source_section = secrets.get('sources', {}).get(source_type, {})
                if not source_section:
                    continue

                # Check credentials based on source type
                if source_type in CREDENTIALS_OBJECT_SOURCES:
                    # Google: look for nested 'credentials' object
                    creds = source_section.get('credentials')
                    if not creds:
                        continue
                else:
                    # Non-Google: flat parameters are the credentials
                    if 'access_token' in source_section or 'api_key' in source_section:
                        creds = source_section
                    else:
                        continue

                try:
                    cred = OAuthCredential(
                        source_type=source_type,
                        provider=meta.get('provider', 'unknown'),
                        identifier=meta.get('identifier', ''),
                        account_info=meta.get('account_info', ''),
                        credentials=creds,
                        created_at=datetime.fromisoformat(meta['created_at']) if meta.get('created_at') else datetime.now(),
                        expires_at=datetime.fromisoformat(meta['expires_at']) if meta.get('expires_at') else None,
                        last_refreshed=datetime.fromisoformat(meta['last_refreshed']) if meta.get('last_refreshed') else None,
                        metadata=meta.get('metadata'),
                    )
                    credentials.append(cred)
                except Exception as e:
                    console.print(f"[yellow]Warning: Could not load {source_type}: {e}[/yellow]")
                    continue

            return credentials

        except Exception as e:
            console.print(f"[red]✗ Failed to list OAuth credentials: {e}[/red]")
            return []

    def delete(self, source_type: str) -> bool:
        """
        Delete OAuth credential for a source type

        Args:
            source_type: dlt source type to delete credentials for

        Returns:
            True if successful, False otherwise
        """
        try:
            secrets = self._load_secrets()

            # Google sources use nested credentials object
            CREDENTIALS_OBJECT_SOURCES = {'google_ads', 'google_analytics', 'google_sheets'}

            # Remove credentials
            if 'sources' in secrets and source_type in secrets['sources']:
                if source_type in CREDENTIALS_OBJECT_SOURCES:
                    # Google: remove nested credentials object
                    if 'credentials' in secrets['sources'][source_type]:
                        del secrets['sources'][source_type]['credentials']
                else:
                    # Non-Google: remove flat credential keys
                    CREDENTIAL_KEYS = {'access_token', 'api_key', 'api_secret', 'refresh_token', 'shop_url'}
                    for key in CREDENTIAL_KEYS:
                        if key in secrets['sources'][source_type]:
                            del secrets['sources'][source_type][key]

                # Clean up empty source section
                if not secrets['sources'][source_type]:
                    del secrets['sources'][source_type]

            # Remove metadata
            if 'dango' in secrets and 'oauth' in secrets['dango']:
                if source_type in secrets['dango']['oauth']:
                    del secrets['dango']['oauth'][source_type]

            self._save_secrets(secrets)
            console.print(f"[green]✓ Deleted OAuth credentials for {source_type}[/green]")
            return True

        except Exception as e:
            console.print(f"[red]✗ Failed to delete OAuth credential: {e}[/red]")
            return False

    def exists(self, source_type: str) -> bool:
        """
        Check if OAuth credentials exist for a source type

        Args:
            source_type: dlt source type

        Returns:
            True if credentials exist
        """
        secrets = self._load_secrets()
        source_section = secrets.get('sources', {}).get(source_type, {})

        # Google sources use nested credentials object
        CREDENTIALS_OBJECT_SOURCES = {'google_ads', 'google_analytics', 'google_sheets'}

        if source_type in CREDENTIALS_OBJECT_SOURCES:
            creds = source_section.get('credentials')
            return creds is not None and 'client_id' in creds
        else:
            # Non-Google: check for common OAuth credential keys
            return 'access_token' in source_section or 'api_key' in source_section
