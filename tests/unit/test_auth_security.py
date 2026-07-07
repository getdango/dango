"""tests/unit/test_auth_security.py

Unit tests for dango.auth.security — password hashing, token generation,
API keys, temporary passwords, and recovery codes.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from pwdlib.exceptions import UnknownHashError

from dango.auth.security import (
    _COMMON_PASSWORDS,
    check_password_strength,
    generate_api_key,
    generate_recovery_codes,
    generate_session_token,
    generate_temp_password,
    get_key_prefix,
    hash_api_key,
    hash_password,
    hash_recovery_code,
    hash_token,
    set_password,
    verify_password,
)


@pytest.mark.unit
class TestPasswordHashing:
    """Tests for hash_password / verify_password."""

    def test_bcrypt_format(self) -> None:
        hashed = hash_password("correcthorsebatterystaple")
        assert hashed.startswith("$2b$12$")

    def test_verify_round_trip(self) -> None:
        password = "my-secure-passphrase!"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_wrong_password_fails(self) -> None:
        hashed = hash_password("real-password")
        assert verify_password("wrong-password", hashed) is False

    def test_random_salt(self) -> None:
        h1 = hash_password("same-password")
        h2 = hash_password("same-password")
        assert h1 != h2

    def test_timing_under_2s(self) -> None:
        start = time.monotonic()
        hash_password("benchmark-password")
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"Hash took {elapsed:.2f}s, expected <2s"

    def test_empty_string(self) -> None:
        hashed = hash_password("")
        assert verify_password("", hashed) is True

    def test_unicode_password(self) -> None:
        password = "\u00e9\u00e8\u00ea\u00eb\u2603\U0001f600"
        hashed = hash_password(password)
        assert verify_password(password, hashed) is True

    def test_invalid_hash_raises(self) -> None:
        with pytest.raises(UnknownHashError):
            verify_password("password", "not-a-bcrypt-hash")


@pytest.mark.unit
class TestCheckPasswordStrength:
    """Tests for check_password_strength (NIST SP 800-63B)."""

    def test_strong_password_no_issues(self) -> None:
        issues = check_password_strength("xK9!mQ2pL#nR")
        assert issues == []

    def test_too_short(self) -> None:
        issues = check_password_strength("short")
        assert any("at least 8" in i for i in issues)

    def test_exactly_8_chars_passes_length(self) -> None:
        issues = check_password_strength("abcdefxy")
        assert not any("at least 8" in i for i in issues)

    def test_7_chars_fails_length(self) -> None:
        issues = check_password_strength("abcdefx")
        assert any("at least 8" in i for i in issues)

    def test_common_password_lowercase(self) -> None:
        issues = check_password_strength("password")
        assert any("too common" in i for i in issues)

    def test_common_password_case_insensitive(self) -> None:
        issues = check_password_strength("PASSWORD")
        assert any("too common" in i for i in issues)

    def test_common_password_mixed_case(self) -> None:
        issues = check_password_strength("PaSsWoRd")
        assert any("too common" in i for i in issues)

    def test_both_issues(self) -> None:
        issues = check_password_strength("admin")
        assert len(issues) == 2
        assert any("at least 8" in i for i in issues)
        assert any("too common" in i for i in issues)

    def test_empty_password(self) -> None:
        issues = check_password_strength("")
        assert any("at least 8" in i for i in issues)

    def test_no_digit_requirement(self) -> None:
        issues = check_password_strength("abcdefghij")
        assert issues == []

    def test_no_uppercase_requirement(self) -> None:
        issues = check_password_strength("alllowercase")
        assert issues == []

    def test_no_special_char_requirement(self) -> None:
        issues = check_password_strength("nospechars1234")
        assert issues == []

    def test_email_none_skips_check(self) -> None:
        # email-like string passed as password (no email= kwarg) — no email checks run
        issues = check_password_strength("user@example.com")
        assert issues == []

    def test_password_equals_email_rejected(self) -> None:
        issues = check_password_strength("user@example.com", email="user@example.com")
        assert any("same as your email" in i for i in issues)

    def test_password_equals_email_case_insensitive(self) -> None:
        issues = check_password_strength("USER@EXAMPLE.COM", email="user@example.com")
        assert any("same as your email" in i for i in issues)

    def test_password_contains_email_username(self) -> None:
        issues = check_password_strength("myuser12345", email="myuser@example.com")
        assert any("email username" in i for i in issues)

    def test_password_contains_email_username_case_insensitive(self) -> None:
        issues = check_password_strength("MYUSER12345", email="myuser@example.com")
        assert any("email username" in i for i in issues)

    def test_email_domain_not_flagged(self) -> None:
        # Domain part alone should not trigger the username check
        issues = check_password_strength("example12345", email="user@example.com")
        assert not any("email" in i for i in issues)

    def test_strong_password_with_email_passes(self) -> None:
        issues = check_password_strength("xK9!mQ2pL#nR", email="user@example.com")
        assert issues == []


@pytest.mark.unit
class TestSessionTokens:
    """Tests for generate_session_token / hash_token."""

    def test_token_length(self) -> None:
        token = generate_session_token()
        assert len(token) == 43

    def test_url_safe_chars(self) -> None:
        token = generate_session_token()
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token)

    def test_uniqueness(self) -> None:
        tokens = {generate_session_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_hash_sha256_format(self) -> None:
        hashed = hash_token("test-token")
        assert len(hashed) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", hashed)

    def test_hash_deterministic(self) -> None:
        assert hash_token("same") == hash_token("same")

    def test_different_tokens_different_hashes(self) -> None:
        t1 = generate_session_token()
        t2 = generate_session_token()
        assert hash_token(t1) != hash_token(t2)


@pytest.mark.unit
class TestAPIKeys:
    """Tests for generate_api_key / hash_api_key / get_key_prefix."""

    def test_prefix_format(self) -> None:
        raw_key, _ = generate_api_key()
        assert raw_key.startswith("dango_ak_")

    def test_returns_tuple(self) -> None:
        result = generate_api_key()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_hash_matches(self) -> None:
        raw_key, stored_hash = generate_api_key()
        assert hash_api_key(raw_key) == stored_hash

    def test_hash_sha256_format(self) -> None:
        _, stored_hash = generate_api_key()
        assert len(stored_hash) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", stored_hash)

    def test_uniqueness(self) -> None:
        keys = {generate_api_key()[0] for _ in range(100)}
        assert len(keys) == 100

    def test_prefix_is_12_chars(self) -> None:
        raw_key, _ = generate_api_key()
        prefix = get_key_prefix(raw_key)
        assert len(prefix) == 12
        assert prefix.startswith("dango_ak_")

    def test_hash_deterministic(self) -> None:
        key = "dango_ak_testkey123"
        assert hash_api_key(key) == hash_api_key(key)


@pytest.mark.unit
class TestTempPasswords:
    """Tests for generate_temp_password (uses secrets.token_urlsafe)."""

    def test_default_length(self) -> None:
        pwd = generate_temp_password()
        # token_urlsafe(16) produces ~22 chars (base64 encoding)
        assert len(pwd) >= 16

    def test_custom_length(self) -> None:
        pwd = generate_temp_password(length=20)
        assert len(pwd) >= 20

    def test_url_safe_chars(self) -> None:
        """Password contains only URL-safe characters (A-Z, a-z, 0-9, -, _)."""
        import re

        for _ in range(100):
            pwd = generate_temp_password()
            assert re.fullmatch(r"[A-Za-z0-9_-]+", pwd), f"Non-URL-safe char in: {pwd}"

    def test_uniqueness(self) -> None:
        passwords = {generate_temp_password() for _ in range(100)}
        assert len(passwords) == 100


@pytest.mark.unit
class TestRecoveryCodes:
    """Tests for generate_recovery_codes / hash_recovery_code."""

    def test_default_count(self) -> None:
        codes = generate_recovery_codes()
        assert len(codes) == 8

    def test_custom_count(self) -> None:
        codes = generate_recovery_codes(count=4)
        assert len(codes) == 4

    def test_format(self) -> None:
        codes = generate_recovery_codes()
        for code in codes:
            assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code), f"Bad format: {code}"

    def test_no_ambiguous_chars(self) -> None:
        ambiguous = set("0O1I")
        for _ in range(10):
            codes = generate_recovery_codes()
            for code in codes:
                chars = code.replace("-", "")
                assert not ambiguous.intersection(chars), f"Ambiguous char in: {code}"

    def test_all_unique(self) -> None:
        codes = generate_recovery_codes(count=8)
        assert len(set(codes)) == len(codes)

    def test_hash_sha256_format(self) -> None:
        hashed = hash_recovery_code("ABCD-EFGH")
        assert len(hashed) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", hashed)

    def test_hash_strips_dashes(self) -> None:
        assert hash_recovery_code("ABCD-EFGH") == hash_recovery_code("ABCDEFGH")

    def test_hash_case_insensitive(self) -> None:
        assert hash_recovery_code("ABCD-EFGH") == hash_recovery_code("abcd-efgh")

    def test_hash_deterministic(self) -> None:
        assert hash_recovery_code("WXYZ-1234") == hash_recovery_code("WXYZ-1234")


@pytest.mark.unit
class TestCommonPasswordsList:
    """Tests for the common passwords frozenset."""

    def test_is_frozenset(self) -> None:
        assert isinstance(_COMMON_PASSWORDS, frozenset)

    def test_has_entries(self) -> None:
        assert len(_COMMON_PASSWORDS) >= 900

    def test_all_lowercase(self) -> None:
        for pwd in _COMMON_PASSWORDS:
            assert pwd == pwd.lower(), f"Non-lowercase entry: {pwd}"

    def test_well_known_passwords_present(self) -> None:
        for pwd in ("password", "123456", "qwerty", "admin", "letmein"):
            assert pwd in _COMMON_PASSWORDS, f"Missing well-known password: {pwd}"


# ---------------------------------------------------------------------------
# set_password() tests
# ---------------------------------------------------------------------------


def _make_auth_db(tmp_path: Path) -> Path:
    """Initialize a fresh auth database via migration runner."""
    from dango.migrations.runner import MigrationRunner

    db_path = tmp_path / "auth.db"
    migrations_dir = Path(__file__).resolve().parents[2] / "dango" / "migrations" / "auth"
    runner = MigrationRunner(db_path=db_path, db_name="auth", migrations_dir=migrations_dir)
    runner.apply_pending()
    return db_path


class TestSetPassword:
    """Tests for set_password()."""

    def test_updates_hash_and_clears_flag(self, tmp_path: Path) -> None:
        """set_password() hashes the password, updates DB, returns updated user."""
        from dango.auth.database import create_user
        from dango.auth.models import Role, User
        from dango.auth.security import verify_password

        db_path = _make_auth_db(tmp_path)
        user = User(email="test@example.com", role=Role.EDITOR, must_change_password=True)
        create_user(db_path, user)

        result = set_password("test@example.com", "newpassword123", db_path)

        assert result.email == "test@example.com"
        assert verify_password("newpassword123", result.password_hash)
        assert result.must_change_password is False
        assert result.password_changed_at is not None

    def test_must_change_flag_true(self, tmp_path: Path) -> None:
        """set_password(must_change_password=True) sets the flag."""
        from dango.auth.database import create_user
        from dango.auth.models import Role, User

        db_path = _make_auth_db(tmp_path)
        user = User(email="test@example.com", role=Role.EDITOR, must_change_password=False)
        create_user(db_path, user)

        result = set_password(
            "test@example.com", "anotherpass1", db_path, must_change_password=True
        )

        assert result.must_change_password is True

    def test_user_not_found_raises(self, tmp_path: Path) -> None:
        """set_password() raises UserNotFoundError when user doesn't exist."""
        from dango.exceptions import UserNotFoundError

        db_path = _make_auth_db(tmp_path)

        with pytest.raises(UserNotFoundError):
            set_password("nonexistent@example.com", "password123", db_path)

    def test_password_changed_at_is_set(self, tmp_path: Path) -> None:
        """set_password() always sets password_changed_at to current time."""
        from datetime import datetime, timezone

        from dango.auth.database import create_user
        from dango.auth.models import Role, User

        db_path = _make_auth_db(tmp_path)
        user = User(email="test@example.com", role=Role.EDITOR)
        create_user(db_path, user)

        before = datetime.now(timezone.utc)
        result = set_password("test@example.com", "newpassword123", db_path)

        assert result.password_changed_at is not None
        assert result.password_changed_at >= before
