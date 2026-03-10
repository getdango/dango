"""dango/platform/notifications/webhook.py

Webhook notification infrastructure for sync events.

Provides event types, configuration models, event filtering, and an async
webhook sender with retry logic.  The sender catches all errors internally —
notification failures never block the sync pipeline.

Wire format uses ``event`` and ``timestamp`` as JSON keys (avoiding structlog
reserved kwargs ``event_type`` and ``occurred_at`` internally).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from dango.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "EVENT_TO_CATEGORY",
    "EventCategory",
    "EventType",
    "NotificationConfig",
    "WebhookConfig",
    "WebhookPayload",
    "WebhookSender",
    "load_notification_config",
    "should_notify",
]

# ---------------------------------------------------------------------------
# Event types and categories
# ---------------------------------------------------------------------------


class EventType(str, Enum):
    """Sync event types that can trigger notifications."""

    SYNC_COMPLETED = "sync_completed"
    SYNC_FAILED = "sync_failed"
    SYNC_STALE = "sync_stale"
    SYNC_RETRYING = "sync_retrying"
    SCHEMA_DRIFT_DETECTED = "schema_drift_detected"


class EventCategory(str, Enum):
    """Broad event categories for notification filtering."""

    SUCCESS = "success"
    FAILURE = "failure"
    STALE = "stale"
    GOVERNANCE = "governance"


EVENT_TO_CATEGORY: dict[EventType, EventCategory] = {
    EventType.SYNC_COMPLETED: EventCategory.SUCCESS,
    EventType.SYNC_FAILED: EventCategory.FAILURE,
    EventType.SYNC_STALE: EventCategory.STALE,
    EventType.SYNC_RETRYING: EventCategory.FAILURE,
    EventType.SCHEMA_DRIFT_DETECTED: EventCategory.GOVERNANCE,
}

# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------


class WebhookConfig(BaseModel):
    """Configuration for a single webhook endpoint."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str
    format: str = "generic"

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            msg = f"Webhook URL must start with http:// or https://, got: {v!r}"
            raise ValueError(msg)
        return v


class NotificationConfig(BaseModel):
    """Global notification settings from ``schedules.yml``."""

    model_config = ConfigDict(frozen=True)

    webhooks: list[WebhookConfig] = []
    on_failure: bool = True
    on_success: bool = False
    on_stale: bool = True
    on_governance: bool = True
    stale_threshold_hours: int = 24


# ---------------------------------------------------------------------------
# Payload model
# ---------------------------------------------------------------------------


class WebhookPayload(BaseModel):
    """Internal representation of a webhook notification payload."""

    model_config = ConfigDict(frozen=True)

    event_type: EventType
    schedule_name: str
    sources: list[str] = []
    error: str | None = None
    duration_seconds: float | None = None
    occurred_at: datetime | None = None
    dashboard_url: str | None = None
    rows_loaded: int | None = None
    stale_hours: float | None = None
    attempt_number: int | None = None
    next_retry_at: datetime | None = None


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_notification_config(project_root: Path) -> NotificationConfig | None:
    """Load notification config from ``.dango/schedules.yml``.

    Returns ``None`` if the file is missing, has no ``notifications`` section,
    or contains invalid YAML.
    """
    path = project_root / ".dango" / "schedules.yml"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        notifications = data.get("notifications")
        if notifications is None:
            return None
        return NotificationConfig(**notifications)
    except Exception:
        logger.warning("failed_to_load_notification_config", path=str(path), exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Event filtering
# ---------------------------------------------------------------------------


def should_notify(
    event_type: EventType,
    config: NotificationConfig,
    schedule_notify_on: dict[str, bool] | None = None,
) -> bool:
    """Determine whether a notification should be sent for an event.

    Per-schedule ``notify_on`` overrides (e.g. ``{"on_success": True}``) take
    precedence over the global config defaults.
    """
    category = EVENT_TO_CATEGORY[event_type]

    key = f"on_{category.value}"

    # Per-schedule override takes precedence
    if schedule_notify_on and key in schedule_notify_on:
        return bool(schedule_notify_on[key])

    # Fall back to global config
    return bool(getattr(config, key, False))


# ---------------------------------------------------------------------------
# Webhook sender
# ---------------------------------------------------------------------------

_TIMEOUT = 10.0
_MAX_RETRIES = 3
_RETRY_DELAYS = [5, 15, 45]

#: Retryable transport errors (connection reset, DNS failure, timeouts).
_RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.ConnectError)


class WebhookSender:
    """Sends webhook notifications to configured endpoints.

    Catches all errors internally — ``send()`` never raises.
    """

    def __init__(self, config: NotificationConfig | None) -> None:
        self._config = config

    @property
    def is_configured(self) -> bool:
        """Whether the sender has any webhooks configured."""
        return self._config is not None and len(self._config.webhooks) > 0

    async def send(
        self,
        *,
        event_type: EventType,
        schedule_name: str,
        sources: list[str] | None = None,
        error: str | None = None,
        duration_seconds: float | None = None,
        dashboard_url: str | None = None,
        rows_loaded: int | None = None,
        stale_hours: float | None = None,
        attempt_number: int | None = None,
        next_retry_at: datetime | None = None,
        schedule_notify_on: dict[str, bool] | None = None,
    ) -> None:
        """Send notifications to all configured webhooks.

        Never raises — all errors are caught and logged.
        """
        if not self.is_configured:
            return

        try:
            assert self._config is not None  # for type narrowing

            if not should_notify(event_type, self._config, schedule_notify_on):
                logger.debug(
                    "notification_filtered",
                    event_type=event_type.value,
                    schedule_name=schedule_name,
                )
                return

            payload = WebhookPayload(
                event_type=event_type,
                schedule_name=schedule_name,
                sources=sources or [],
                error=error,
                duration_seconds=duration_seconds,
                occurred_at=datetime.now(tz=timezone.utc),
                dashboard_url=dashboard_url,
                rows_loaded=rows_loaded,
                stale_hours=stale_hours,
                attempt_number=attempt_number,
                next_retry_at=next_retry_at,
            )

            results = await asyncio.gather(
                *(self._send_to_webhook(wh, payload) for wh in self._config.webhooks),
                return_exceptions=True,
            )

            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.warning(
                        "webhook_send_exception",
                        webhook=self._config.webhooks[i].name,
                        error=str(result),
                    )
        except Exception:
            logger.warning(
                "webhook_send_unexpected_error",
                schedule_name=schedule_name,
                exc_info=True,
            )

    def _format_payload(self, webhook: WebhookConfig, payload: WebhookPayload) -> dict[str, Any]:
        """Select the appropriate wire format for a webhook."""
        if webhook.format == "slack":
            from dango.platform.notifications.slack import format_slack_message

            return format_slack_message(payload)
        return self._build_json_payload(payload)

    async def _send_to_webhook(self, webhook: WebhookConfig, payload: WebhookPayload) -> bool:
        """Send payload to a single webhook with retry logic.

        Retries up to 3 times with exponential backoff (5s, 15s, 45s) on
        server errors (5xx), timeouts, and connection errors.
        """
        json_payload = self._format_payload(webhook, payload)
        for attempt in range(_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(webhook.url, json=json_payload)

                if resp.status_code < 300:
                    logger.info(
                        "webhook_delivered",
                        webhook=webhook.name,
                        status=resp.status_code,
                    )
                    return True

                if resp.status_code >= 500:
                    logger.warning(
                        "webhook_server_error",
                        webhook=webhook.name,
                        status=resp.status_code,
                        attempt=attempt + 1,
                    )
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(_RETRY_DELAYS[attempt])
                        continue
                    return False

                # 4xx — don't retry
                logger.warning(
                    "webhook_client_error",
                    webhook=webhook.name,
                    status=resp.status_code,
                )
                return False

            except _RETRYABLE_ERRORS:
                logger.warning(
                    "webhook_transport_error",
                    webhook=webhook.name,
                    attempt=attempt + 1,
                    exc_info=True,
                )
                if attempt < _MAX_RETRIES - 1:
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                    continue
                return False

            except Exception:
                logger.warning(
                    "webhook_send_error",
                    webhook=webhook.name,
                    attempt=attempt + 1,
                    exc_info=True,
                )
                return False

        return False

    @staticmethod
    def _build_json_payload(payload: WebhookPayload) -> dict[str, Any]:
        """Convert internal payload to wire-format JSON.

        Maps internal field names to the public API:
        - ``event_type`` → ``event``
        - ``occurred_at`` → ``timestamp``
        """
        result: dict[str, Any] = {
            "event": payload.event_type.value,
            "schedule": payload.schedule_name,
            "sources": payload.sources,
            "error": payload.error,
            "duration_seconds": payload.duration_seconds,
            "timestamp": payload.occurred_at.isoformat() if payload.occurred_at else None,
            "dashboard_url": payload.dashboard_url,
            "rows_loaded": payload.rows_loaded,
            "stale_hours": payload.stale_hours,
            "attempt_number": payload.attempt_number,
            "next_retry_at": (payload.next_retry_at.isoformat() if payload.next_retry_at else None),
        }
        return result
