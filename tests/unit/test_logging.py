"""tests/unit/test_logging.py

Tests for dango.logging — structured logging configuration and public API.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import pytest
import structlog

from dango.logging import (
    bind_contextvars,
    clear_contextvars,
    configure_logging,
    get_logger,
    unbind_contextvars,
)


@pytest.fixture(autouse=True)
def _reset_logging():
    """Reset logging state between tests to ensure isolation."""
    yield

    # Clear contextvars
    clear_contextvars()

    # Remove all handlers from root logger
    root = logging.getLogger()
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    # Reset structlog to defaults
    structlog.reset_defaults()


@pytest.mark.unit
class TestConfigureLogging:
    def test_creates_log_directory(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        assert log_dir.exists()

    def test_creates_log_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        assert (log_dir / "dango.log").exists()

    def test_creates_file_handler(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "_DangoFileHandler" in handler_types

    def test_creates_console_handler(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "StreamHandler" in handler_types

    def test_writes_json_to_file(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        logger = get_logger("test")
        logger.info("test_event", key="value")

        log_file = log_dir / "dango.log"
        content = log_file.read_text().strip()
        assert content  # not empty
        record = json.loads(content)
        assert record["event"] == "test_event"
        assert record["key"] == "value"

    def test_idempotent_reconfiguration(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        configure_logging(log_dir=log_dir, json_console=True, log_level="DEBUG")

        # Should still be functional after reconfiguration
        logger = get_logger("test")
        logger.info("after_reconfig")

        log_file = log_dir / "dango.log"
        lines = [line for line in log_file.read_text().strip().split("\n") if line]
        events = [json.loads(line)["event"] for line in lines]
        assert "after_reconfig" in events

    def test_invalid_level_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid log level"):
            configure_logging(log_dir=tmp_path / "logs", log_level="INVALID")

    def test_fallback_on_unwritable_directory(self, tmp_path: Path) -> None:
        # Use a path that cannot be created (file instead of dir)
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file")
        bad_log_dir = blocker / "logs"  # Can't mkdir inside a file

        # Should not raise — falls back to console-only
        with pytest.warns(RuntimeWarning, match="Cannot write to log directory"):
            configure_logging(log_dir=bad_log_dir, json_console=True)

        # File handler should NOT be present
        root = logging.getLogger()
        handler_types = [type(h).__name__ for h in root.handlers]
        assert "_DangoFileHandler" not in handler_types
        assert "StreamHandler" in handler_types


@pytest.mark.unit
class TestGetLogger:
    def test_returns_logger_with_standard_methods(self, tmp_path: Path) -> None:
        configure_logging(log_dir=tmp_path / "logs", json_console=True)
        logger = get_logger("test.module")
        assert callable(logger.debug)
        assert callable(logger.info)
        assert callable(logger.warning)
        assert callable(logger.error)
        assert callable(logger.critical)

    def test_preserves_logger_name_in_output(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        logger = get_logger("my.custom.name")
        logger.info("name_test")

        log_file = log_dir / "dango.log"
        record = json.loads(log_file.read_text().strip())
        assert record["logger"] == "my.custom.name"


@pytest.mark.unit
class TestLogLevelConfiguration:
    def test_default_level_is_info(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        root = logging.getLogger()
        assert root.level == logging.INFO

    def test_explicit_override(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True, log_level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG

    def test_env_var_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DANGO_LOG_LEVEL", "WARNING")
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        root = logging.getLogger()
        assert root.level == logging.WARNING

    def test_explicit_beats_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DANGO_LOG_LEVEL", "WARNING")
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True, log_level="ERROR")
        root = logging.getLogger()
        assert root.level == logging.ERROR

    def test_case_insensitive(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True, log_level="debug")
        root = logging.getLogger()
        assert root.level == logging.DEBUG


@pytest.mark.unit
class TestJSONOutput:
    def test_required_fields_present(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        logger = get_logger("fields.test")
        logger.info("check_fields")

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert "timestamp" in record
        assert "level" in record
        assert "logger" in record
        assert "event" in record

    def test_correct_level_values(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True, log_level="DEBUG")
        logger = get_logger("levels.test")

        logger.debug("d")
        logger.info("i")
        logger.warning("w")
        logger.error("e")

        lines = (log_dir / "dango.log").read_text().strip().split("\n")
        records = [json.loads(line) for line in lines]
        levels = [r["level"] for r in records]
        assert "debug" in levels
        assert "info" in levels
        assert "warning" in levels
        assert "error" in levels

    def test_structlog_kv_pairs_in_json(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        logger = get_logger("kv.test")
        logger.info("sync_done", source="stripe", rows=42)

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert record["source"] == "stripe"
        assert record["rows"] == 42


@pytest.mark.unit
class TestFileRotation:
    def _get_file_handler(self, tmp_path: Path) -> logging.Handler:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        root = logging.getLogger()
        timed_handlers = [
            h for h in root.handlers if isinstance(h, logging.handlers.TimedRotatingFileHandler)
        ]
        assert len(timed_handlers) == 1
        return timed_handlers[0]

    def test_is_timed_rotating_handler(self, tmp_path: Path) -> None:
        handler = self._get_file_handler(tmp_path)
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)

    def test_rotates_daily_at_midnight(self, tmp_path: Path) -> None:
        handler = self._get_file_handler(tmp_path)
        assert handler.when == "MIDNIGHT"

    def test_backup_count_configured(self, tmp_path: Path) -> None:
        handler = self._get_file_handler(tmp_path)
        assert handler.backupCount == 30

    def test_max_bytes_configured(self, tmp_path: Path) -> None:
        handler = self._get_file_handler(tmp_path)
        assert handler.maxBytes == 10 * 1024 * 1024

    def test_gzip_namer_configured(self, tmp_path: Path) -> None:
        handler = self._get_file_handler(tmp_path)
        assert handler.namer is not None
        # Namer should append .gz
        assert handler.namer("dango.log.20260303") == "dango.log.20260303.gz"

    def test_gzip_rotator_configured(self, tmp_path: Path) -> None:
        handler = self._get_file_handler(tmp_path)
        assert handler.rotator is not None


@pytest.mark.unit
class TestCorrelationIds:
    def test_bind_adds_fields_to_output(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        bind_contextvars(request_id="req-123", user_id="usr-456")
        logger = get_logger("ctx.test")
        logger.info("with_context")

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert record["request_id"] == "req-123"
        assert record["user_id"] == "usr-456"

    def test_clear_removes_all_context(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        bind_contextvars(request_id="req-123")
        clear_contextvars()
        logger = get_logger("ctx.test")
        logger.info("after_clear")

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert "request_id" not in record

    def test_unbind_removes_specific_keys(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        bind_contextvars(request_id="req-123", user_id="usr-456")
        unbind_contextvars("request_id")
        logger = get_logger("ctx.test")
        logger.info("partial_unbind")

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert "request_id" not in record
        assert record["user_id"] == "usr-456"


@pytest.mark.unit
class TestStdlibIntegration:
    def test_stdlib_logger_produces_structured_json(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        stdlib_logger = logging.getLogger("stdlib.test")
        stdlib_logger.info("stdlib_event")

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert record["event"] == "stdlib_event"
        assert "timestamp" in record
        assert "level" in record

    def test_stdlib_logger_includes_bound_correlation_ids(self, tmp_path: Path) -> None:
        log_dir = tmp_path / "logs"
        configure_logging(log_dir=log_dir, json_console=True)
        bind_contextvars(trace_id="trace-abc")
        stdlib_logger = logging.getLogger("stdlib.corr")
        stdlib_logger.info("stdlib_with_ctx")

        record = json.loads((log_dir / "dango.log").read_text().strip())
        assert record["trace_id"] == "trace-abc"
