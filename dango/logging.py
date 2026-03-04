"""dango/logging.py

Structured logging infrastructure for Dango (structlog + stdlib integration).

Provides JSON-formatted file logging and human-readable console logging.
Wraps stdlib logging so existing ``logging.getLogger(__name__)`` calls in
web/app.py and metabase.py automatically produce structured output once
``configure_logging()`` has been called.

Public API:
    configure_logging   — one-time setup (call from entry points)
    get_logger          — convenience wrapper for structlog.get_logger
    bind_contextvars    — add correlation IDs / request context
    clear_contextvars   — clear all bound context
    unbind_contextvars  — remove specific context keys
"""

from __future__ import annotations

import gzip
import logging
import logging.config
import logging.handlers
import os
import re
import shutil
import warnings
from pathlib import Path
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)

__all__ = [
    "configure_logging",
    "get_logger",
    "bind_contextvars",
    "clear_contextvars",
    "unbind_contextvars",
]

# Defaults
_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_DEFAULT_BACKUP_COUNT = 30  # 30 daily archives

# Pattern for the date suffix appended by TimedRotatingFileHandler
_DATE_SUFFIX_RE = re.compile(r"^\d{8}\.gz$", re.ASCII)


class _DangoFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Daily rotation with gzip compression and mid-day size guard.

    Combines ``TimedRotatingFileHandler`` (daily at midnight) with a file-size
    check so that runaway logging mid-day still triggers a rotation.  Archives
    are gzip-compressed with a compact ``YYYYMMDD`` date suffix.
    """

    def __init__(self, *args: Any, maxBytes: int = 0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.maxBytes = maxBytes
        # Override suffix/extMatch for YYYYMMDD.gz naming
        self.suffix = "%Y%m%d"
        self.extMatch = _DATE_SUFFIX_RE
        self.namer = self._gzip_namer
        self.rotator = self._gzip_rotator

    # -- naming / compression callbacks ------------------------------------

    @staticmethod
    def _gzip_namer(name: str) -> str:
        """Append ``.gz`` to the rotated filename."""
        return name + ".gz"

    @staticmethod
    def _gzip_rotator(source: str, dest: str) -> None:
        """Gzip-compress *source* into *dest*, then remove *source*."""
        with open(source, "rb") as f_in:
            with gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        os.remove(source)

    # -- rollover trigger --------------------------------------------------

    def shouldRollover(self, record: logging.LogRecord) -> int:  # noqa: N802
        """Check both time and size triggers for rotation."""
        if super().shouldRollover(record):
            return 1
        # Mid-day size guard
        if self.maxBytes > 0 and self.stream:
            self.stream.seek(0, 2)  # seek to end
            if self.stream.tell() >= self.maxBytes:
                return 1
        return 0


def _resolve_log_level(log_level: str | None) -> str:
    """Resolve log level: explicit arg > DANGO_LOG_LEVEL env var > INFO default."""
    level = log_level or os.environ.get("DANGO_LOG_LEVEL") or _DEFAULT_LOG_LEVEL
    level = level.upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        msg = f"Invalid log level: {level!r}. Must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL."
        raise ValueError(msg)
    return level


def _build_shared_processors() -> list[structlog.types.Processor]:
    """Build the structlog processor chain shared by all handlers."""
    return [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]


def configure_logging(
    *,
    log_level: str | None = None,
    log_dir: Path | None = None,
    json_console: bool = False,
) -> None:
    """Configure structured logging for Dango.

    Safe to call multiple times — reconfigures with new settings.

    Args:
        log_level: Logging level. Falls back to ``DANGO_LOG_LEVEL`` env var,
            then ``"INFO"``.
        log_dir: Directory for ``dango.log``. Defaults to
            ``.dango/logs`` relative to cwd. If the directory is not writable,
            falls back to console-only logging with a warning.
        json_console: If True, console output uses JSON. Otherwise uses
            structlog's ``ConsoleRenderer`` (human-readable, colored when TTY).
    """
    level = _resolve_log_level(log_level)

    # Resolve log directory
    if log_dir is None:
        log_dir = Path.cwd() / ".dango" / "logs"

    # --- Build handlers ---
    handlers: dict[str, dict] = {}
    shared_processors = _build_shared_processors()

    # File handler — JSON output
    file_handler_ok = _setup_file_handler(handlers, log_dir, level)

    # Console handler — human-readable or JSON
    if json_console:
        console_formatter = "json"
    else:
        console_formatter = "console"

    handlers["console"] = {
        "class": "logging.StreamHandler",
        "formatter": console_formatter,
        "stream": "ext://sys.stderr",
        "level": level,
    }

    # --- stdlib logging config ---
    handler_names = list(handlers.keys())
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.JSONRenderer(),
                    ],
                    "foreign_pre_chain": shared_processors,
                },
                "console": {
                    "()": structlog.stdlib.ProcessorFormatter,
                    "processors": [
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.dev.ConsoleRenderer(),
                    ],
                    "foreign_pre_chain": shared_processors,
                },
            },
            "handlers": handlers,
            "root": {
                "handlers": handler_names,
                "level": level,
            },
        }
    )

    # --- structlog configuration ---
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    if not file_handler_ok:
        logger = get_logger("dango.logging")
        logger.warning(
            "file_logging_unavailable",
            log_dir=str(log_dir),
            reason="directory not writable",
        )


def _setup_file_handler(
    handlers: dict[str, dict],
    log_dir: Path,
    level: str,
) -> bool:
    """Try to create the file handler. Returns True on success."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "dango.log"
        # Verify we can write
        log_file.touch(exist_ok=True)
    except OSError:
        warnings.warn(
            f"Cannot write to log directory {log_dir}. Falling back to console-only logging.",
            RuntimeWarning,
            stacklevel=3,
        )
        return False

    handlers["file"] = {
        "()": _DangoFileHandler,
        "formatter": "json",
        "filename": str(log_file),
        "when": "midnight",
        "backupCount": _DEFAULT_BACKUP_COUNT,
        "maxBytes": _DEFAULT_MAX_BYTES,
        "level": level,
        "encoding": "utf-8",
    }
    return True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Get a structured logger.

    Convenience wrapper around ``structlog.get_logger()``. The returned logger
    supports all standard log methods (debug, info, warning, error, critical)
    plus structlog's key-value binding.

    Args:
        name: Logger name — typically ``__name__`` for the calling module.

    Returns:
        A structlog ``BoundLogger`` instance.
    """
    return structlog.get_logger(name)  # type: ignore[no-any-return]
