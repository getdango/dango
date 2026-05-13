"""dango/security/token_storage.py

Encrypts OAuth tokens using OS keychain for key storage and Fernet for encryption. Provides secure storage and retrieval of sensitive credentials.
"""

import json
from pathlib import Path
from typing import Any

import keyring
from cryptography.fernet import Fernet
from rich.console import Console

console = Console()


class SecureTokenStorage:
    """
    Secure storage for OAuth tokens using OS keychain and Fernet encryption

    Storage approach:
    - Master encryption key stored in OS keychain (macOS Keychain, Windows Credential Manager, Linux Secret Service)
    - Tokens encrypted with Fernet symmetric encryption
    - Encrypted data saved to .dlt/secrets.toml

    For cloud deployment (v1.0): Replace keychain with cloud secrets manager
    """

    SERVICE_NAME = "dango-oauth"
    KEY_NAME = "master-encryption-key"

    def __init__(self, project_root: Path):
        """
        Initialize secure token storage

        Args:
            project_root: Project root directory containing .dlt/
        """
        self.project_root = Path(project_root)
        self.dlt_dir = self.project_root / ".dlt"
        self.dlt_dir.mkdir(parents=True, exist_ok=True)

    def _get_encryption_key(self) -> bytes:
        """
        Get or create master encryption key from OS keychain

        Returns:
            Encryption key as bytes
        """
        try:
            # Try to get existing key from keychain
            key_str = keyring.get_password(self.SERVICE_NAME, self.KEY_NAME)

            if not key_str:
                # Generate new key
                key = Fernet.generate_key()
                key_str = key.decode("utf-8")

                # Save to keychain
                keyring.set_password(self.SERVICE_NAME, self.KEY_NAME, key_str)
                console.print("[dim]Created new encryption key in OS keychain[/dim]")

            return key_str.encode("utf-8")

        except Exception as e:
            import logging

            _logger = logging.getLogger(__name__)
            _logger.info("OS keychain unavailable, using file-based encryption key: %s", e)
            console.print(f"[yellow]Warning: Could not access OS keychain: {e}[/yellow]")
            console.print(
                "[yellow]Using file-based encryption key "
                "(key stored at .dlt/.encryption_key with restricted permissions)[/yellow]"
            )
            # Fallback: Use a project-specific key (less secure but works)
            key_file = self.dlt_dir / ".encryption_key"
            if key_file.exists():
                return key_file.read_bytes()
            else:
                key = Fernet.generate_key()
                # TOCTOU: file is briefly world-readable between write_bytes
                # and chmod. For a more secure pattern, use os.open() with
                # mode at creation time (see cloud/ssh.py generate_key()).
                key_file.write_bytes(key)
                key_file.chmod(0o600)  # Restrict permissions
                return key

    def encrypt_token(self, token_data: dict[str, Any]) -> str:
        """
        Encrypt token data

        Args:
            token_data: Dictionary of token data to encrypt

        Returns:
            Encrypted token as base64 string
        """
        try:
            key = self._get_encryption_key()
            f = Fernet(key)

            # Serialize to JSON and encrypt
            json_data = json.dumps(token_data).encode("utf-8")
            encrypted = f.encrypt(json_data)

            return encrypted.decode("utf-8")

        except Exception as e:
            console.print(f"[red]Encryption error: {e}[/red]")
            raise

    def decrypt_token(self, encrypted_data: str) -> dict[str, Any]:
        """
        Decrypt token data

        Args:
            encrypted_data: Encrypted token as base64 string

        Returns:
            Decrypted token data as dictionary
        """
        try:
            key = self._get_encryption_key()
            f = Fernet(key)

            # Decrypt and deserialize
            decrypted = f.decrypt(encrypted_data.encode("utf-8"))
            token_data: dict[str, Any] = json.loads(decrypted.decode("utf-8"))

            return token_data

        except Exception as e:
            console.print(f"[red]Decryption error: {e}[/red]")
            raise

    def is_encrypted(self, data: str) -> bool:
        """
        Check if data appears to be encrypted

        Args:
            data: String to check

        Returns:
            True if data looks encrypted, False otherwise
        """
        # Fernet tokens start with "gAAAAA" when base64 encoded
        return data.startswith("gAAAAA")

    def rotate_encryption_key(self) -> bool:
        """
        Rotate the encryption key (re-encrypt all tokens with new key)

        This is a security best practice to perform periodically.

        Returns:
            True if successful, False otherwise
        """
        try:
            console.print("[cyan]Rotating encryption key...[/cyan]")

            # TODO: Implement key rotation
            # 1. Get all encrypted tokens from .dlt/secrets.toml
            # 2. Decrypt with old key
            # 3. Generate new key
            # 4. Re-encrypt with new key
            # 5. Save new key to keychain
            # 6. Update .dlt/secrets.toml

            console.print("[yellow]Key rotation not yet implemented[/yellow]")
            return False

        except Exception as e:
            console.print(f"[red]Key rotation failed: {e}[/red]")
            return False
