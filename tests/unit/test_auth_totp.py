"""tests/unit/test_auth_totp.py

Unit tests for dango/auth/totp.py — TOTP 2FA business logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyotp
import pytest

from dango.auth import database as db
from dango.auth.models import Role, User
from dango.auth.security import hash_password, hash_recovery_code
from dango.auth.totp import (
    consume_recovery_code,
    disable_totp,
    enable_totp,
    generate_totp_secret,
    get_provisioning_uri,
    hash_and_store_codes,
    regenerate_recovery_codes,
    setup_totp,
    verify_recovery_code,
    verify_totp_code,
)
from dango.migrations.runner import MigrationRunner

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create a fresh auth database."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    db_path = dango_dir / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return db_path


def _make_user(db_path: Path, **overrides: Any) -> User:
    """Create and persist a test user."""
    defaults: dict[str, Any] = {
        "email": "test@example.com",
        "password_hash": hash_password("securepassword123"),
        "role": Role.EDITOR,
    }
    defaults.update(overrides)
    user = User(**defaults)
    db.create_user(db_path, user)
    return user


# ---------------------------------------------------------------------------
# Pure function tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGenerateTotpSecret:
    """Tests for generate_totp_secret()."""

    def test_returns_base32_string(self) -> None:
        secret = generate_totp_secret()
        assert secret
        assert len(secret) == 32
        # Valid base32 characters
        valid = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=")
        assert set(secret).issubset(valid)

    def test_unique_secrets(self) -> None:
        secrets = {generate_totp_secret() for _ in range(10)}
        assert len(secrets) == 10


@pytest.mark.unit
class TestGetProvisioningUri:
    """Tests for get_provisioning_uri()."""

    def test_format(self) -> None:
        secret = generate_totp_secret()
        uri = get_provisioning_uri(secret, "user@example.com")
        assert uri.startswith("otpauth://totp/")
        assert "user%40example.com" in uri or "user@example.com" in uri
        assert "Dango" in uri

    def test_custom_issuer(self) -> None:
        secret = generate_totp_secret()
        uri = get_provisioning_uri(secret, "user@example.com", issuer="CustomApp")
        assert "CustomApp" in uri

    def test_contains_secret(self) -> None:
        secret = generate_totp_secret()
        uri = get_provisioning_uri(secret, "user@example.com")
        assert f"secret={secret}" in uri


@pytest.mark.unit
class TestVerifyTotpCode:
    """Tests for verify_totp_code()."""

    def test_valid_code(self) -> None:
        secret = generate_totp_secret()
        totp = pyotp.TOTP(secret)
        code = totp.now()
        assert verify_totp_code(secret, code) is True

    def test_invalid_code(self) -> None:
        secret = generate_totp_secret()
        assert verify_totp_code(secret, "000000") is False

    def test_empty_code(self) -> None:
        secret = generate_totp_secret()
        assert verify_totp_code(secret, "") is False

    def test_empty_secret(self) -> None:
        assert verify_totp_code("", "123456") is False

    def test_non_digit_code(self) -> None:
        secret = generate_totp_secret()
        assert verify_totp_code(secret, "abcdef") is False

    def test_wrong_length_code(self) -> None:
        secret = generate_totp_secret()
        assert verify_totp_code(secret, "12345") is False
        assert verify_totp_code(secret, "1234567") is False

    def test_code_with_spaces_stripped(self) -> None:
        secret = generate_totp_secret()
        totp = pyotp.TOTP(secret)
        code = totp.now()
        # Leading/trailing spaces should be stripped
        assert verify_totp_code(secret, f" {code} ") is True


@pytest.mark.unit
class TestVerifyRecoveryCode:
    """Tests for verify_recovery_code()."""

    def test_match_found(self) -> None:
        codes = ["ABCD-EFGH", "JKLM-NPQR"]
        hashes = [hash_recovery_code(c) for c in codes]
        stored_json = json.dumps(hashes)

        matched, updated = verify_recovery_code(stored_json, "ABCD-EFGH")
        assert matched is True
        assert updated is not None
        remaining = json.loads(updated)
        assert len(remaining) == 1
        assert remaining[0] == hashes[1]

    def test_no_match(self) -> None:
        hashes = [hash_recovery_code("ABCD-EFGH")]
        stored_json = json.dumps(hashes)

        matched, updated = verify_recovery_code(stored_json, "XXXX-YYYY")
        assert matched is False
        assert updated is None

    def test_case_insensitive(self) -> None:
        hashes = [hash_recovery_code("ABCD-EFGH")]
        stored_json = json.dumps(hashes)

        matched, _ = verify_recovery_code(stored_json, "abcd-efgh")
        assert matched is True

    def test_without_dashes(self) -> None:
        hashes = [hash_recovery_code("ABCD-EFGH")]
        stored_json = json.dumps(hashes)

        matched, _ = verify_recovery_code(stored_json, "ABCDEFGH")
        assert matched is True

    def test_none_input(self) -> None:
        matched, updated = verify_recovery_code(None, "ABCD-EFGH")
        assert matched is False
        assert updated is None

    def test_empty_list(self) -> None:
        matched, updated = verify_recovery_code("[]", "ABCD-EFGH")
        assert matched is False
        assert updated is None

    def test_empty_code(self) -> None:
        hashes = [hash_recovery_code("ABCD-EFGH")]
        matched, updated = verify_recovery_code(json.dumps(hashes), "")
        assert matched is False
        assert updated is None

    def test_invalid_json(self) -> None:
        matched, updated = verify_recovery_code("not json", "ABCD-EFGH")
        assert matched is False
        assert updated is None


@pytest.mark.unit
class TestHashAndStoreCodes:
    """Tests for hash_and_store_codes()."""

    def test_correct_count(self) -> None:
        codes = ["AAAA-BBBB", "CCCC-DDDD", "EEEE-FFFF"]
        result = hash_and_store_codes(codes)
        hashes = json.loads(result)
        assert len(hashes) == 3

    def test_valid_hashes(self) -> None:
        codes = ["AAAA-BBBB"]
        result = hash_and_store_codes(codes)
        hashes = json.loads(result)
        # SHA-256 hex is 64 chars
        assert len(hashes[0]) == 64

    def test_round_trip_with_verify(self) -> None:
        codes = ["ABCD-EFGH", "JKLM-NPQR"]
        stored = hash_and_store_codes(codes)
        matched, _ = verify_recovery_code(stored, "ABCD-EFGH")
        assert matched is True


# ---------------------------------------------------------------------------
# Database operation tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSetupTotp:
    """Tests for setup_totp()."""

    def test_stores_secret_and_codes(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        secret = generate_totp_secret()
        codes = ["AAAA-BBBB", "CCCC-DDDD"]

        setup_totp(db_path, user.id, secret, codes)

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.totp_secret == secret
        assert updated.totp_enabled is False
        assert updated.recovery_codes is not None
        stored_hashes = json.loads(updated.recovery_codes)
        assert len(stored_hashes) == 2


@pytest.mark.unit
class TestEnableTotp:
    """Tests for enable_totp()."""

    def test_flips_enabled(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        secret = generate_totp_secret()
        setup_totp(db_path, user.id, secret, ["AAAA-BBBB"])

        enable_totp(db_path, user.id)

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.totp_enabled is True


@pytest.mark.unit
class TestDisableTotp:
    """Tests for disable_totp()."""

    def test_clears_fields(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        secret = generate_totp_secret()
        setup_totp(db_path, user.id, secret, ["AAAA-BBBB"])
        enable_totp(db_path, user.id)

        disable_totp(db_path, user.id)

        updated = db.get_user_by_id(db_path, user.id)
        assert updated is not None
        assert updated.totp_secret is None
        assert updated.totp_enabled is False
        assert updated.recovery_codes is None


@pytest.mark.unit
class TestConsumeRecoveryCode:
    """Tests for consume_recovery_code()."""

    def test_consumes_valid_code(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        codes = ["AAAA-BBBB", "CCCC-DDDD"]
        setup_totp(db_path, user.id, generate_totp_secret(), codes)

        # Re-read to get stored hashes
        u = db.get_user_by_id(db_path, user.id)
        assert u is not None
        result = consume_recovery_code(db_path, user.id, u.recovery_codes, "AAAA-BBBB")
        assert result is True

        # Only one code should remain
        u2 = db.get_user_by_id(db_path, user.id)
        assert u2 is not None
        remaining = json.loads(u2.recovery_codes)  # type: ignore[arg-type]
        assert len(remaining) == 1

    def test_rejects_invalid_code(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        codes = ["AAAA-BBBB"]
        setup_totp(db_path, user.id, generate_totp_secret(), codes)

        u = db.get_user_by_id(db_path, user.id)
        assert u is not None
        result = consume_recovery_code(db_path, user.id, u.recovery_codes, "XXXX-YYYY")
        assert result is False


@pytest.mark.unit
class TestRegenerateRecoveryCodes:
    """Tests for regenerate_recovery_codes()."""

    def test_returns_new_codes(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        setup_totp(db_path, user.id, generate_totp_secret(), ["AAAA-BBBB"])

        new_codes = regenerate_recovery_codes(db_path, user.id)
        assert len(new_codes) == 8  # Default count
        # All formatted as XXXX-XXXX
        for code in new_codes:
            assert len(code) == 9
            assert code[4] == "-"

    def test_replaces_old_codes(self, tmp_path: Path) -> None:
        db_path = _make_db(tmp_path)
        user = _make_user(db_path)
        setup_totp(db_path, user.id, generate_totp_secret(), ["AAAA-BBBB"])

        regenerate_recovery_codes(db_path, user.id)
        u = db.get_user_by_id(db_path, user.id)
        assert u is not None
        hashes = json.loads(u.recovery_codes)  # type: ignore[arg-type]
        assert len(hashes) == 8

        # Old code should no longer work
        matched, _ = verify_recovery_code(u.recovery_codes, "AAAA-BBBB")
        assert matched is False
