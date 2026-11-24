"""dango/security/token_storage.py

Encrypts OAuth tokens using OS keychain for key storage and Fernet for encryption. Provides secure storage and retrieval of sensitive credentials.
"""

import json
import os
from pathlib import Path
from typing import Any

import keyring
from cryptography.fernet import Fernet, InvalidToken
from rich.console import Console
from rich.markup import escape

console = Console()


class TokenKeyError(InvalidToken):
    """No available key can decrypt a stored value; the message names the cause and recovery step."""


_REPAIR_STEP = (
    "If the key cannot be recovered, run 'dango auth metabase-repair <email>' to regenerate "
    "that user's Metabase bridge password."
)


def _no_matching_key_message(found_keys: bool, keychain_error: Exception | None) -> str:
    if keychain_error is not None:
        return (
            "The encryption key could not be found: the OS keychain could not be read (access "
            "denied, locked or unavailable) and the .dlt/.encryption_key file "
            + ("does not open this value." if found_keys else "does not exist.")
            + " Allow Dango to use the keychain and try again. "
            + _REPAIR_STEP
        )
    if found_keys:
        return (
            "The stored encryption key does not match this value (it may have been written under "
            "a different keychain, project copy or earlier run). " + _REPAIR_STEP
        )
    return (
        "No encryption key was found for this value: the OS keychain has none and there is no "
        ".dlt/.encryption_key file (was the project moved or restored without its keychain?). "
        + _REPAIR_STEP
    )


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

    @property
    def _key_file(self) -> Path:
        return self.dlt_dir / ".encryption_key"

    def _read_keychain_key(self) -> tuple[bytes | None, Exception | None]:
        """Return ``(key, None)``, ``(None, None)`` when the keychain has no key, or ``(None, error)``."""
        try:
            key_str = keyring.get_password(self.SERVICE_NAME, self.KEY_NAME)
        except Exception as exc:
            return None, exc
        return (key_str.encode("utf-8") if key_str else None), None

    def _read_file_key(self) -> bytes | None:
        """Return the project's fallback key file contents, or ``None`` when there is none."""
        if self._key_file.exists():
            return self._key_file.read_bytes()
        return None

    def _get_encryption_key(self) -> bytes:
        """
        Get the key to encrypt with: the OS keychain key, else an existing file key, else a new one

        Both places are consulted before a key is generated, so a key written while the
        keychain was unavailable (or the other way round) is never silently replaced.

        Returns:
            Encryption key as bytes
        """
        key, keychain_error = self._read_keychain_key()
        if key is not None:
            return key

        file_key = self._read_file_key()
        if file_key is not None:
            if keychain_error is not None:
                self._warn_file_key(keychain_error)
            return file_key

        if keychain_error is None:
            try:
                new_key = Fernet.generate_key()
                keyring.set_password(self.SERVICE_NAME, self.KEY_NAME, new_key.decode("utf-8"))
                console.print("[dim]Created new encryption key in OS keychain[/dim]")
                return new_key
            except Exception as exc:
                keychain_error = exc

        self._warn_file_key(keychain_error)
        # Fallback: Use a project-specific key (less secure but works)
        key = Fernet.generate_key()
        # Atomic: file created with 0o600 permissions from the start
        fd = os.open(str(self._key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    def _warn_file_key(self, error: Exception) -> None:
        import logging

        logging.getLogger(__name__).info(
            "OS keychain unavailable, using file-based encryption key: %s", error
        )
        console.print(
            f"[yellow]Warning: Could not access OS keychain: {escape(str(error))}[/yellow]"
        )
        console.print(
            "[yellow]Using file-based encryption key "
            "(key stored at .dlt/.encryption_key with restricted permissions)[/yellow]"
        )

    def _decryption_candidates(self) -> tuple[list[bytes], Exception | None]:
        """Every key that may have encrypted a value, keychain first. Never creates a key."""
        keys: list[bytes] = []
        key, keychain_error = self._read_keychain_key()
        if key is not None:
            keys.append(key)
        file_key = self._read_file_key()
        if file_key is not None and file_key not in keys:
            keys.append(file_key)
        return keys, keychain_error

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
        keys, keychain_error = self._decryption_candidates()
        try:
            for key in keys:
                try:
                    decrypted = Fernet(key).decrypt(encrypted_data.encode("utf-8"))
                except InvalidToken:
                    continue
                token_data: dict[str, Any] = json.loads(decrypted.decode("utf-8"))
                return token_data
            raise TokenKeyError(_no_matching_key_message(bool(keys), keychain_error))
        except TokenKeyError as e:
            console.print(f"[red]Decryption error: {escape(str(e))}[/red]")
            raise
        except Exception as e:
            console.print(f"[red]Decryption error: {escape(str(e))}[/red]")
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
