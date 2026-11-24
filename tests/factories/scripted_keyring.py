"""tests/factories/scripted_keyring.py

An in-memory keyring whose answer can be changed between calls (allow, deny, locked, missing), raising the exception classes the real macOS and Windows backends raise. It records service, username and operation, never a value.
"""

from __future__ import annotations

import keyring.backend
import keyring.errors


class WindowsCredentialError(Exception):
    """Stand-in for ``pywintypes.error`` (a plain ``Exception``, not a ``KeyringError``).

    ``WinVaultKeyring`` re-raises it for every Credential Manager failure other than
    "not found" (winerror 1168), for example access denied (5) or no logon session (1312).
    """


class ScriptedKeyring(keyring.backend.KeyringBackend):
    """Keyring that behaves as ``mode`` says; switch ``mode`` between calls to script a user's day.

    Modes:
    - ``allow``: works.
    - ``deny``: the user pressed Deny. macOS ``api.KeychainDenied`` becomes ``KeyringLocked`` on
      get and set; delete raises ``PasswordDeleteError`` (any ``api.Error``).
    - ``locked``: ``KeyringLocked`` on every operation.
    - ``secauth``: macOS ``SecAuthFailure``: ``KeyringError`` on get, ``PasswordSetError`` on set.
    - ``windows``: ``WindowsCredentialError`` on every operation.
    - ``missing``: ``NoKeyringError`` on every operation (no backend installed).
    - ``drop_writes``: set "succeeds" but stores nothing.
    """

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.mode = "allow"
        self.store: dict[tuple[str, str], str] = {}
        self.ops: list[tuple[str, str, str]] = []

    def _record(self, op: str, service: str, username: str) -> None:
        self.ops.append((op, service, username))

    def writes(self) -> list[tuple[str, str, str]]:
        """Recorded set/delete operations (what changed the keychain)."""
        return [op for op in self.ops if op[0] != "get"]

    def get_password(self, service: str, username: str) -> str | None:
        self._record("get", service, username)
        if self.mode == "deny" or self.mode == "locked":
            raise keyring.errors.KeyringLocked("Can't get password from keychain: denied")
        if self.mode == "secauth":
            raise keyring.errors.KeyringError("Can't get password from keychain: auth failure")
        if self.mode == "windows":
            raise WindowsCredentialError(5, "CredRead", "Access is denied.")
        if self.mode == "missing":
            raise keyring.errors.NoKeyringError("No recommended backend was available.")
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._record("set", service, username)
        if self.mode == "deny" or self.mode == "locked":
            raise keyring.errors.KeyringLocked("Can't store password on keychain: denied")
        if self.mode == "secauth":
            raise keyring.errors.PasswordSetError("Can't store password on keychain")
        if self.mode == "windows":
            raise WindowsCredentialError(5, "CredWrite", "Access is denied.")
        if self.mode == "missing":
            raise keyring.errors.NoKeyringError("No recommended backend was available.")
        if self.mode == "drop_writes":
            return
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._record("delete", service, username)
        if self.mode in ("deny", "secauth"):
            raise keyring.errors.PasswordDeleteError("Can't delete password in keychain")
        if self.mode == "locked":
            raise keyring.errors.KeyringLocked("Can't delete password in keychain")
        if self.mode == "windows":
            raise WindowsCredentialError(5, "CredDelete", "Access is denied.")
        if self.mode == "missing":
            raise keyring.errors.NoKeyringError("No recommended backend was available.")
        try:
            del self.store[(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(username) from None
