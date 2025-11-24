"""
OAuth Credential Storage

Manages OAuth credentials with separate [oauth.*] sections in .dlt/secrets.toml.
Provides auto-naming, expiry tracking, and credential lifecycle management.
"""

import re
import toml
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any
from rich.console import Console

from dango.security import SecureTokenStorage

console = Console()


@dataclass
class OAuthCredential:
    """
    OAuth credential with metadata

    Attributes:
        name: Auto-generated name (e.g., "oauth.google_business_company_com")
        provider: Provider type (e.g., "google", "facebook_ads", "shopify")
        identifier: Provider-specific identifier (email, account_id, shop_url)
        account_info: Human-readable account description
        credentials: Token data (encrypted)
        created_at: When credential was created
        expires_at: When credential expires (None for non-expiring)
        last_used: Last time credential was used
        last_refreshed: Last time token was refreshed (for auto-refresh providers)
        metadata: Additional provider-specific metadata
    """
    name: str
    provider: str
    identifier: str
    account_info: str
    credentials: Dict[str, Any]
    created_at: datetime
    expires_at: Optional[datetime] = None
    last_used: Optional[datetime] = None
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

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization"""
        data = asdict(self)
        # Convert datetime objects to ISO format strings
        if data['created_at']:
            data['created_at'] = data['created_at'].isoformat()
        if data['expires_at']:
            data['expires_at'] = data['expires_at'].isoformat()
        if data['last_used']:
            data['last_used'] = data['last_used'].isoformat()
        if data['last_refreshed']:
            data['last_refreshed'] = data['last_refreshed'].isoformat()
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'OAuthCredential':
        """Create from dictionary"""
        # Convert ISO format strings back to datetime objects
        if isinstance(data.get('created_at'), str):
            data['created_at'] = datetime.fromisoformat(data['created_at'])
        if isinstance(data.get('expires_at'), str):
            data['expires_at'] = datetime.fromisoformat(data['expires_at'])
        if isinstance(data.get('last_used'), str):
            data['last_used'] = datetime.fromisoformat(data['last_used'])
        if isinstance(data.get('last_refreshed'), str):
            data['last_refreshed'] = datetime.fromisoformat(data['last_refreshed'])
        return cls(**data)


class OAuthStorage:
    """
    OAuth credential storage manager

    Manages [oauth.*] sections in .dlt/secrets.toml with encryption and metadata tracking.
    """

    def __init__(self, project_root: Path, use_encryption: bool = True):
        """
        Initialize OAuth storage

        Args:
            project_root: Project root directory
            use_encryption: Whether to encrypt tokens (default: True)
        """
        self.project_root = Path(project_root)
        self.dlt_dir = self.project_root / ".dlt"
        self.secrets_file = self.dlt_dir / "secrets.toml"
        self.use_encryption = use_encryption

        if use_encryption:
            self.token_storage = SecureTokenStorage(project_root)
        else:
            self.token_storage = None

        # Ensure .dlt directory exists
        self.dlt_dir.mkdir(parents=True, exist_ok=True)

        # Create secrets.toml if it doesn't exist
        if not self.secrets_file.exists():
            self.secrets_file.write_text("")

    def _sanitize_name(self, name: str) -> str:
        """
        Sanitize identifier for use in TOML key

        Args:
            name: Original identifier

        Returns:
            Sanitized identifier safe for TOML keys
        """
        # Replace invalid characters with underscores
        sanitized = re.sub(r'[^a-zA-Z0-9_-]', '_', name)
        # Remove consecutive underscores
        sanitized = re.sub(r'__+', '_', sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip('_')
        return sanitized.lower()

    def generate_oauth_name(self, provider: str, identifier: str) -> str:
        """
        Generate OAuth credential name from provider and identifier

        Args:
            provider: Provider type (e.g., "google", "facebook_ads")
            identifier: Provider-specific identifier

        Returns:
            Auto-generated OAuth name (e.g., "google_business_company_com")
        """
        sanitized_id = self._sanitize_name(identifier)
        return f"{provider}_{sanitized_id}"

    def save(self, oauth_cred: OAuthCredential) -> bool:
        """
        Save OAuth credential to .dlt/secrets.toml

        Args:
            oauth_cred: OAuth credential to save

        Returns:
            True if successful, False otherwise
        """
        try:
            # Load existing secrets
            if self.secrets_file.exists() and self.secrets_file.stat().st_size > 0:
                secrets = toml.load(self.secrets_file)
            else:
                secrets = {}

            # Ensure oauth section exists
            if 'oauth' not in secrets:
                secrets['oauth'] = {}

            # Prepare credential data
            cred_data = oauth_cred.to_dict()

            # Encrypt credentials if encryption enabled
            if self.use_encryption and self.token_storage:
                encrypted = self.token_storage.encrypt_token(cred_data['credentials'])
                cred_data['credentials'] = {
                    '_encrypted': True,
                    '_data': encrypted
                }

            # Save to oauth section
            secrets['oauth'][oauth_cred.name] = cred_data

            # Write back to file
            with open(self.secrets_file, 'w') as f:
                toml.dump(secrets, f)

            console.print(f"[green]✓ Saved OAuth credential: {oauth_cred.name}[/green]")
            return True

        except Exception as e:
            console.print(f"[red]✗ Failed to save OAuth credential: {e}[/red]")
            return False

    def get(self, oauth_name: str) -> Optional[OAuthCredential]:
        """
        Get OAuth credential by name

        Args:
            oauth_name: OAuth credential name

        Returns:
            OAuthCredential if found, None otherwise
        """
        try:
            if not self.secrets_file.exists():
                return None

            secrets = toml.load(self.secrets_file)

            # Check oauth section
            if 'oauth' not in secrets or oauth_name not in secrets['oauth']:
                return None

            cred_data = secrets['oauth'][oauth_name]

            # Decrypt credentials if encrypted
            if isinstance(cred_data.get('credentials'), dict) and cred_data['credentials'].get('_encrypted'):
                if self.token_storage:
                    encrypted_data = cred_data['credentials']['_data']
                    cred_data['credentials'] = self.token_storage.decrypt_token(encrypted_data)
                else:
                    console.print(f"[red]Cannot decrypt {oauth_name}: encryption not available[/red]")
                    return None

            return OAuthCredential.from_dict(cred_data)

        except Exception as e:
            console.print(f"[red]✗ Failed to load OAuth credential {oauth_name}: {e}[/red]")
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
            if not self.secrets_file.exists():
                return []

            secrets = toml.load(self.secrets_file)

            if 'oauth' not in secrets:
                return []

            credentials = []
            for oauth_name, cred_data in secrets['oauth'].items():
                # Filter by provider if specified
                if provider and cred_data.get('provider') != provider:
                    continue

                # Decrypt credentials if encrypted
                if isinstance(cred_data.get('credentials'), dict) and cred_data['credentials'].get('_encrypted'):
                    if self.token_storage:
                        encrypted_data = cred_data['credentials']['_data']
                        cred_data['credentials'] = self.token_storage.decrypt_token(encrypted_data)
                    else:
                        # Skip encrypted credentials if can't decrypt
                        continue

                try:
                    cred = OAuthCredential.from_dict(cred_data)
                    credentials.append(cred)
                except Exception as e:
                    console.print(f"[yellow]Warning: Could not load {oauth_name}: {e}[/yellow]")
                    continue

            return credentials

        except Exception as e:
            console.print(f"[red]✗ Failed to list OAuth credentials: {e}[/red]")
            return []

    def delete(self, oauth_name: str) -> bool:
        """
        Delete OAuth credential

        Args:
            oauth_name: OAuth credential name to delete

        Returns:
            True if successful, False otherwise
        """
        try:
            if not self.secrets_file.exists():
                console.print(f"[yellow]OAuth credential {oauth_name} not found[/yellow]")
                return False

            secrets = toml.load(self.secrets_file)

            if 'oauth' not in secrets or oauth_name not in secrets['oauth']:
                console.print(f"[yellow]OAuth credential {oauth_name} not found[/yellow]")
                return False

            # Delete the credential
            del secrets['oauth'][oauth_name]

            # Remove oauth section if empty
            if not secrets['oauth']:
                del secrets['oauth']

            # Write back to file
            with open(self.secrets_file, 'w') as f:
                toml.dump(secrets, f)

            console.print(f"[green]✓ Deleted OAuth credential: {oauth_name}[/green]")
            return True

        except Exception as e:
            console.print(f"[red]✗ Failed to delete OAuth credential: {e}[/red]")
            return False

    def update_last_used(self, oauth_name: str) -> bool:
        """
        Update last_used timestamp for OAuth credential

        Args:
            oauth_name: OAuth credential name

        Returns:
            True if successful, False otherwise
        """
        try:
            cred = self.get(oauth_name)
            if not cred:
                return False

            cred.last_used = datetime.now()
            return self.save(cred)

        except Exception as e:
            console.print(f"[red]✗ Failed to update last_used: {e}[/red]")
            return False

    def find_by_provider_and_identifier(self, provider: str, identifier: str) -> Optional[OAuthCredential]:
        """
        Find OAuth credential by provider and identifier

        Args:
            provider: Provider type
            identifier: Provider-specific identifier

        Returns:
            OAuthCredential if found, None otherwise
        """
        credentials = self.list(provider=provider)
        for cred in credentials:
            if cred.identifier == identifier:
                return cred
        return None
