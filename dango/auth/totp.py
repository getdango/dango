"""dango/auth/totp.py

TOTP two-factor authentication business logic.

Provides secret generation, provisioning URI construction, code verification,
recovery code management, and database persistence for TOTP 2FA.  Pure
functions have no side effects; DB functions take ``db_path`` and operate
on the auth SQLite database.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyotp

from dango.auth.audit import AuditEvent, log_auth_event
from dango.auth.database import get_user_by_id, update_user
from dango.auth.models import UserUpdate
from dango.auth.security import generate_recovery_codes, hash_recovery_code

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def generate_totp_secret() -> str:
    """Generate a random base32 TOTP secret.

    Returns:
        A 32-character base32-encoded secret string.
    """
    return pyotp.random_base32()


def get_provisioning_uri(secret: str, email: str, issuer: str = "Dango") -> str:
    """Build an ``otpauth://`` provisioning URI for QR code display.

    Args:
        secret: Base32-encoded TOTP secret.
        email: User email (used as the account name in authenticator apps).
        issuer: Application name shown in the authenticator.

    Returns:
        Full ``otpauth://totp/...`` URI.
    """
    totp = pyotp.TOTP(secret)
    return totp.provisioning_uri(name=email, issuer_name=issuer)


def verify_totp_code(secret: str, code: str) -> bool:
    """Verify a TOTP code against a secret with a +-30s drift window.

    Returns ``False`` for empty/malformed input rather than raising.

    Args:
        secret: Base32-encoded TOTP secret.
        code: 6-digit TOTP code to verify.

    Returns:
        ``True`` if the code is valid within ``valid_window=1``.
    """
    if not secret or not code:
        return False
    # Must be exactly 6 digits
    stripped = code.strip()
    if not stripped.isdigit() or len(stripped) != 6:
        return False
    totp = pyotp.TOTP(secret)
    return totp.verify(stripped, valid_window=1)


def verify_recovery_code(stored_hashes_json: str | None, code: str) -> tuple[bool, str | None]:
    """Check a recovery code against stored hashes.

    If the code matches, returns ``(True, updated_json)`` with the used
    hash removed.  If not, returns ``(False, None)``.

    Args:
        stored_hashes_json: JSON array of SHA-256 hex hashes, or ``None``.
        code: Raw recovery code (any case, with or without dashes).

    Returns:
        Tuple of ``(matched, updated_hashes_json_or_none)``.
    """
    if not stored_hashes_json or not code:
        return False, None

    try:
        hashes: list[str] = json.loads(stored_hashes_json)
    except (json.JSONDecodeError, TypeError):
        return False, None

    if not hashes:
        return False, None

    code_hash = hash_recovery_code(code)
    for i, stored in enumerate(hashes):
        if stored == code_hash:
            remaining = hashes[:i] + hashes[i + 1 :]
            return True, json.dumps(remaining)

    return False, None


def hash_and_store_codes(codes: list[str]) -> str:
    """Hash recovery codes and return a JSON array of hex digests.

    Args:
        codes: Raw recovery code strings.

    Returns:
        JSON-encoded array of SHA-256 hex hashes.
    """
    hashes = [hash_recovery_code(c) for c in codes]
    return json.dumps(hashes)


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------


def setup_totp(db_path: Path, user_id: str, secret: str, codes: list[str]) -> None:
    """Store a TOTP secret and hashed recovery codes (not yet enabled).

    The secret is persisted immediately but ``totp_enabled`` stays
    ``False`` until the user verifies a code via ``enable_totp()``.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: User ID to update.
        secret: Base32-encoded TOTP secret.
        codes: Raw recovery codes (will be hashed before storage).
    """
    hashed_json = hash_and_store_codes(codes)
    update_user(
        db_path,
        user_id,
        UserUpdate(
            totp_secret=secret,
            totp_enabled=False,
            recovery_codes=hashed_json,
        ),
    )


def enable_totp(db_path: Path, user_id: str) -> None:
    """Flip ``totp_enabled`` to ``True`` and log the event.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: User ID to enable 2FA for.
    """
    update_user(db_path, user_id, UserUpdate(totp_enabled=True))
    user = get_user_by_id(db_path, user_id)
    email = user.email if user else None
    log_auth_event(AuditEvent.TWO_FA_ENABLED, user_id=user_id, email=email)


def disable_totp(db_path: Path, user_id: str) -> None:
    """Clear all TOTP fields and log the event.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: User ID to disable 2FA for.
    """
    user = get_user_by_id(db_path, user_id)
    email = user.email if user else None
    update_user(
        db_path,
        user_id,
        UserUpdate(
            totp_secret=None,
            totp_enabled=False,
            recovery_codes=None,
        ),
    )
    log_auth_event(AuditEvent.TWO_FA_DISABLED, user_id=user_id, email=email)


def consume_recovery_code(
    db_path: Path, user_id: str, stored_hashes_json: str | None, code: str
) -> bool:
    """Verify a recovery code and remove it from the database if valid.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: User ID.
        stored_hashes_json: Current JSON array of hashed recovery codes.
        code: Raw recovery code to check.

    Returns:
        ``True`` if the code was valid and consumed.
    """
    matched, updated_json = verify_recovery_code(stored_hashes_json, code)
    if not matched:
        return False
    update_user(db_path, user_id, UserUpdate(recovery_codes=updated_json))
    user = get_user_by_id(db_path, user_id)
    email = user.email if user else None
    log_auth_event(AuditEvent.RECOVERY_CODE_USED, user_id=user_id, email=email)
    return True


def regenerate_recovery_codes(db_path: Path, user_id: str) -> list[str]:
    """Generate new recovery codes, replacing any existing ones.

    Args:
        db_path: Path to the auth SQLite database.
        user_id: User ID.

    Returns:
        List of raw (unhashed) recovery codes to display to the user.
    """
    codes = generate_recovery_codes()
    hashed_json = hash_and_store_codes(codes)
    update_user(db_path, user_id, UserUpdate(recovery_codes=hashed_json))
    return codes
