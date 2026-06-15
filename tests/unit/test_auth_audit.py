"""tests/unit/test_auth_audit.py

Tests for security audit logging in dango/auth/audit.py.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.auth.audit import (
    AuditEvent,
    get_audit_log_path,
    log_auth_event,
    query_audit_log,
)

# -- AuditEvent enum -------------------------------------------------------


@pytest.mark.unit
class TestAuditEvent:
    """Validate AuditEvent taxonomy and type properties."""

    def test_member_count(self) -> None:
        """AuditEvent has at least 21 members (grows as events are added)."""
        assert len(AuditEvent) >= 21

    def test_values_are_lowercase_snake_case(self) -> None:
        """All values use lowercase_snake_case (no uppercase, no hyphens)."""
        for member in AuditEvent:
            assert member.value == member.value.lower(), f"{member.name} value not lowercase"
            assert "-" not in member.value, f"{member.name} value contains hyphens"
            assert member.value.replace("_", "").isalpha(), (
                f"{member.name} value has non-alpha chars"
            )

    def test_is_str_subclass(self) -> None:
        """AuditEvent members are str instances (str, Enum pattern)."""
        assert isinstance(AuditEvent.LOGIN_SUCCESS, str)
        assert AuditEvent.LOGIN_SUCCESS == "login_success"


# -- get_audit_log_path -----------------------------------------------------


@pytest.mark.unit
class TestGetAuditLogPath:
    """Validate audit log path resolution."""

    def test_default_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default path is .dango/logs/audit.jsonl under cwd."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".dango").mkdir()
        result = get_audit_log_path()
        assert result == tmp_path / ".dango" / "logs" / "audit.jsonl"

    def test_custom_log_dir(self, tmp_path: Path) -> None:
        """Custom log_dir overrides default."""
        result = get_audit_log_path(log_dir=tmp_path)
        assert result == tmp_path / "audit.jsonl"

    def test_creates_directory(self, tmp_path: Path) -> None:
        """Directory is created if it doesn't exist."""
        nested = tmp_path / "deep" / "nested"
        result = get_audit_log_path(log_dir=nested)
        assert result.parent.is_dir()
        assert result == nested / "audit.jsonl"


# -- log_auth_event ---------------------------------------------------------


@pytest.mark.unit
class TestLogAuthEvent:
    """Validate audit event writing."""

    def test_basic_write(self, tmp_path: Path) -> None:
        """A basic event is written as a single JSONL line."""
        log_auth_event(AuditEvent.LOGIN_SUCCESS, user_id="u1", email="a@b.com", log_dir=tmp_path)
        log_path = tmp_path / "audit.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().splitlines()
        assert len(lines) == 1
        entry: dict[str, Any] = json.loads(lines[0])
        assert entry["event"] == "login_success"
        assert entry["user_id"] == "u1"
        assert entry["email"] == "a@b.com"

    def test_all_fields_present(self, tmp_path: Path) -> None:
        """When all optional fields are provided, all appear in the entry."""
        log_auth_event(
            AuditEvent.ROLE_CHANGED,
            user_id="u1",
            email="a@b.com",
            ip="192.168.1.1",
            details={"old_role": "viewer", "new_role": "editor"},
            log_dir=tmp_path,
        )
        entry: dict[str, Any] = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        assert entry["event"] == "role_changed"
        assert entry["user_id"] == "u1"
        assert entry["email"] == "a@b.com"
        assert entry["ip"] == "192.168.1.1"
        assert entry["details"] == {"old_role": "viewer", "new_role": "editor"}
        assert "timestamp" in entry

    def test_minimal_event_omits_none_fields(self, tmp_path: Path) -> None:
        """When optional fields are None they are omitted from output."""
        log_auth_event(AuditEvent.RATE_LIMIT_HIT, log_dir=tmp_path)
        entry: dict[str, Any] = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        assert "user_id" not in entry
        assert "email" not in entry
        assert "ip" not in entry
        assert "details" not in entry
        assert entry["event"] == "rate_limit_hit"

    def test_details_dict(self, tmp_path: Path) -> None:
        """Arbitrary details dict is serialized correctly."""
        log_auth_event(
            AuditEvent.API_KEY_CREATED,
            user_id="u2",
            details={"key_name": "ci-bot", "prefix": "dango_ak_abc"},
            log_dir=tmp_path,
        )
        entry: dict[str, Any] = json.loads((tmp_path / "audit.jsonl").read_text().strip())
        assert entry["details"]["key_name"] == "ci-bot"

    def test_auto_creates_file(self, tmp_path: Path) -> None:
        """audit.jsonl is created on first write."""
        log_path = tmp_path / "audit.jsonl"
        assert not log_path.exists()
        log_auth_event(AuditEvent.LOGOUT, user_id="u1", log_dir=tmp_path)
        assert log_path.exists()

    def test_appends_multiple(self, tmp_path: Path) -> None:
        """Multiple calls append to the same file."""
        log_auth_event(AuditEvent.LOGIN_SUCCESS, user_id="u1", log_dir=tmp_path)
        log_auth_event(AuditEvent.LOGIN_FAILURE, email="bad@x.com", log_dir=tmp_path)
        log_auth_event(AuditEvent.ACCOUNT_LOCKED, user_id="u1", log_dir=tmp_path)
        lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[0])["event"] == "login_success"
        assert json.loads(lines[1])["event"] == "login_failure"
        assert json.loads(lines[2])["event"] == "account_locked"

    def test_emits_structlog(self, tmp_path: Path) -> None:
        """log_auth_event calls the structlog logger at INFO level."""
        with patch("dango.auth.audit._logger") as mock_logger:
            log_auth_event(AuditEvent.USER_CREATED, user_id="u1", email="a@b.com", log_dir=tmp_path)
            mock_logger.info.assert_called_once()
            call_args = mock_logger.info.call_args
            assert call_args[0][0] == "audit_event"
            assert call_args[1]["audit_event"] == "user_created"

    def test_graceful_failure_on_unwritable(self, tmp_path: Path) -> None:
        """Write failure is swallowed; a warning is emitted instead."""
        bad_dir = tmp_path / "nonexistent"
        # Don't create the directory — get_audit_log_path will create it,
        # but we can make the file unwritable by writing a directory at
        # the file path.
        bad_dir.mkdir()
        (bad_dir / "audit.jsonl").mkdir()  # directory where file should be
        with patch("dango.auth.audit._logger") as mock_logger:
            # Should not raise
            log_auth_event(AuditEvent.LOGIN_FAILURE, log_dir=bad_dir)
            mock_logger.warning.assert_called_once()
            assert mock_logger.warning.call_args[0][0] == "audit_log_write_failed"


# -- query_audit_log --------------------------------------------------------


@pytest.mark.unit
class TestQueryAuditLog:
    """Validate audit log reading and filtering."""

    def _write_entries(self, log_dir: Path, entries: list[dict[str, Any]]) -> None:
        """Write raw JSONL entries to audit.jsonl."""
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "audit.jsonl"
        with open(log_path, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def test_empty_no_file(self, tmp_path: Path) -> None:
        """Returns [] when audit.jsonl doesn't exist."""
        result = query_audit_log(log_dir=tmp_path)
        assert result == []

    def test_empty_file(self, tmp_path: Path) -> None:
        """Returns [] for an empty file."""
        self._write_entries(tmp_path, [])
        result = query_audit_log(log_dir=tmp_path)
        assert result == []

    def test_round_trip(self, tmp_path: Path) -> None:
        """Events written with log_auth_event are readable via query."""
        log_auth_event(AuditEvent.LOGIN_SUCCESS, user_id="u1", email="a@b.com", log_dir=tmp_path)
        results = query_audit_log(log_dir=tmp_path)
        assert len(results) == 1
        assert results[0]["event"] == "login_success"
        assert results[0]["user_id"] == "u1"

    def test_filter_by_event_type(self, tmp_path: Path) -> None:
        """event_type filter keeps only matching entries."""
        self._write_entries(
            tmp_path,
            [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event": "login_success",
                    "user_id": "u1",
                },
                {
                    "timestamp": "2026-01-01T00:01:00+00:00",
                    "event": "login_failure",
                    "email": "bad@x.com",
                },
                {
                    "timestamp": "2026-01-01T00:02:00+00:00",
                    "event": "login_success",
                    "user_id": "u2",
                },
            ],
        )
        results = query_audit_log(event_type=AuditEvent.LOGIN_SUCCESS, log_dir=tmp_path)
        assert len(results) == 2
        assert all(r["event"] == "login_success" for r in results)

    def test_filter_by_user_id(self, tmp_path: Path) -> None:
        """user_id filter keeps only that user's entries."""
        self._write_entries(
            tmp_path,
            [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event": "login_success",
                    "user_id": "u1",
                },
                {"timestamp": "2026-01-01T00:01:00+00:00", "event": "logout", "user_id": "u2"},
                {
                    "timestamp": "2026-01-01T00:02:00+00:00",
                    "event": "password_change",
                    "user_id": "u1",
                },
            ],
        )
        results = query_audit_log(user_id="u1", log_dir=tmp_path)
        assert len(results) == 2
        assert all(r["user_id"] == "u1" for r in results)

    def test_filter_by_since(self, tmp_path: Path) -> None:
        """since filter excludes entries before the threshold."""
        self._write_entries(
            tmp_path,
            [
                {"timestamp": "2026-01-01T00:00:00+00:00", "event": "login_success"},
                {"timestamp": "2026-01-02T00:00:00+00:00", "event": "logout"},
                {"timestamp": "2026-01-03T00:00:00+00:00", "event": "password_change"},
            ],
        )
        results = query_audit_log(since="2026-01-02T00:00:00+00:00", log_dir=tmp_path)
        assert len(results) == 2
        events = {r["event"] for r in results}
        assert events == {"logout", "password_change"}

    def test_combined_filters(self, tmp_path: Path) -> None:
        """Multiple filters are ANDed together."""
        self._write_entries(
            tmp_path,
            [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "event": "login_success",
                    "user_id": "u1",
                },
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "event": "login_success",
                    "user_id": "u1",
                },
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "event": "login_failure",
                    "user_id": "u1",
                },
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "event": "login_success",
                    "user_id": "u2",
                },
            ],
        )
        results = query_audit_log(
            since="2026-01-02T00:00:00+00:00",
            event_type=AuditEvent.LOGIN_SUCCESS,
            user_id="u1",
            log_dir=tmp_path,
        )
        assert len(results) == 1
        assert results[0]["user_id"] == "u1"
        assert results[0]["event"] == "login_success"

    def test_limit(self, tmp_path: Path) -> None:
        """limit truncates results after filtering."""
        entries = [
            {
                "timestamp": f"2026-01-{i + 1:02d}T00:00:00+00:00",
                "event": "login_success",
                "user_id": "u1",
            }
            for i in range(10)
        ]
        self._write_entries(tmp_path, entries)
        results = query_audit_log(limit=3, log_dir=tmp_path)
        assert len(results) == 3

    def test_newest_first(self, tmp_path: Path) -> None:
        """Results are ordered newest-first."""
        self._write_entries(
            tmp_path,
            [
                {"timestamp": "2026-01-01T00:00:00+00:00", "event": "login_success"},
                {"timestamp": "2026-01-02T00:00:00+00:00", "event": "logout"},
                {"timestamp": "2026-01-03T00:00:00+00:00", "event": "password_change"},
            ],
        )
        results = query_audit_log(log_dir=tmp_path)
        assert results[0]["timestamp"] == "2026-01-03T00:00:00+00:00"
        assert results[-1]["timestamp"] == "2026-01-01T00:00:00+00:00"

    def test_corrupt_line_skipped(self, tmp_path: Path) -> None:
        """Corrupt JSON lines are skipped with a warning, not raised."""
        log_dir = tmp_path
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "audit.jsonl"
        log_path.write_text(
            '{"timestamp":"2026-01-01T00:00:00+00:00","event":"login_success","user_id":"u1"}\n'
            "this is not json\n"
            '{"timestamp":"2026-01-02T00:00:00+00:00","event":"logout","user_id":"u2"}\n'
        )
        with patch("dango.auth.audit._logger") as mock_logger:
            results = query_audit_log(log_dir=tmp_path)
        assert len(results) == 2
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][0] == "audit_log_corrupt_line"

    def test_unreadable_file_returns_empty(self, tmp_path: Path) -> None:
        """Returns [] and warns when the file cannot be read."""
        log_path = tmp_path / "audit.jsonl"
        log_path.write_text('{"timestamp":"2026-01-01T00:00:00+00:00","event":"login_success"}\n')
        log_path.chmod(0o000)
        try:
            with patch("dango.auth.audit._logger") as mock_logger:
                results = query_audit_log(log_dir=tmp_path)
            assert results == []
            mock_logger.warning.assert_called_once()
            assert mock_logger.warning.call_args[0][0] == "audit_log_read_failed"
        finally:
            log_path.chmod(0o644)  # restore for tmp_path cleanup

    def test_limit_applies_after_filter(self, tmp_path: Path) -> None:
        """Limit is applied after filtering, not before."""
        entries = [
            {
                "timestamp": f"2026-01-{i + 1:02d}T00:00:00+00:00",
                "event": "login_success",
                "user_id": "u1",
            }
            for i in range(5)
        ] + [
            {"timestamp": f"2026-01-{i + 6:02d}T00:00:00+00:00", "event": "logout", "user_id": "u2"}
            for i in range(5)
        ]
        self._write_entries(tmp_path, entries)
        results = query_audit_log(event_type=AuditEvent.LOGIN_SUCCESS, limit=3, log_dir=tmp_path)
        assert len(results) == 3
        assert all(r["event"] == "login_success" for r in results)


# -- Package exports --------------------------------------------------------


@pytest.mark.unit
class TestExports:
    """Validate that dango.auth re-exports audit public API."""

    def test_public_names_in_auth_package(self) -> None:
        """All 4 public names are accessible from dango.auth."""
        import dango.auth

        for name in ("AuditEvent", "get_audit_log_path", "log_auth_event", "query_audit_log"):
            assert hasattr(dango.auth, name), f"dango.auth missing {name}"
            assert name in dango.auth.__all__, f"{name} not in dango.auth.__all__"
