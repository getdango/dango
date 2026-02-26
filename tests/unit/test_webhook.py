"""tests/unit/test_webhook.py

Tests for webhook notification infrastructure in
dango/platform/notifications/webhook.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import yaml

from dango.platform.notifications.webhook import (
    EVENT_TO_CATEGORY,
    EventCategory,
    EventType,
    NotificationConfig,
    WebhookConfig,
    WebhookSender,
    load_notification_config,
    should_notify,
)

# Patch targets at origin module
_HTTPX_CLIENT = "dango.platform.notifications.webhook.httpx.AsyncClient"
_ASYNC_SLEEP = "dango.platform.notifications.webhook.asyncio.sleep"


def _make_config(**overrides: Any) -> NotificationConfig:
    """Build a NotificationConfig with sensible defaults."""
    defaults: dict[str, Any] = {
        "webhooks": [
            {"name": "test-hook", "url": "https://example.com/hook"},
        ],
        "on_failure": True,
        "on_success": False,
        "on_stale": True,
    }
    defaults.update(overrides)
    return NotificationConfig(**defaults)


def _mock_httpx_client(response: MagicMock | None = None) -> AsyncMock:
    """Create a mock httpx.AsyncClient context manager."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _resp(status: int = 200) -> MagicMock:
    """Build a mock httpx response with the given status code."""
    r = MagicMock()
    r.status_code = status
    return r


def _write_schedules(tmp_path: Path, notifications: dict[str, Any]) -> Path:
    """Write a schedules.yml with a notifications section."""
    d = tmp_path / ".dango"
    d.mkdir(exist_ok=True)
    data: dict[str, Any] = {"notifications": notifications}
    (d / "schedules.yml").write_text(yaml.safe_dump(data))
    return tmp_path


# ---------------------------------------------------------------------------
# EventType → EventCategory mapping
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEventMapping:
    """Each event type maps to the correct category."""

    def test_sync_completed(self) -> None:
        assert EVENT_TO_CATEGORY[EventType.SYNC_COMPLETED] == EventCategory.SUCCESS

    def test_sync_failed(self) -> None:
        assert EVENT_TO_CATEGORY[EventType.SYNC_FAILED] == EventCategory.FAILURE

    def test_sync_stale(self) -> None:
        assert EVENT_TO_CATEGORY[EventType.SYNC_STALE] == EventCategory.STALE

    def test_sync_retrying(self) -> None:
        assert EVENT_TO_CATEGORY[EventType.SYNC_RETRYING] == EventCategory.FAILURE


# ---------------------------------------------------------------------------
# should_notify
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShouldNotify:
    """Event filtering with global defaults and per-schedule overrides."""

    def test_default_failure_enabled(self) -> None:
        config = _make_config(on_failure=True)
        assert should_notify(EventType.SYNC_FAILED, config) is True

    def test_default_success_disabled(self) -> None:
        config = _make_config(on_success=False)
        assert should_notify(EventType.SYNC_COMPLETED, config) is False

    def test_default_stale_enabled(self) -> None:
        config = _make_config(on_stale=True)
        assert should_notify(EventType.SYNC_STALE, config) is True

    def test_schedule_override_enables(self) -> None:
        config = _make_config(on_success=False)
        override = {"on_success": True}
        assert should_notify(EventType.SYNC_COMPLETED, config, override) is True

    def test_schedule_override_disables(self) -> None:
        config = _make_config(on_failure=True)
        override = {"on_failure": False}
        assert should_notify(EventType.SYNC_FAILED, config, override) is False

    def test_schedule_override_absent_falls_to_global(self) -> None:
        config = _make_config(on_failure=True)
        override = {"on_success": True}  # unrelated key
        assert should_notify(EventType.SYNC_FAILED, config, override) is True


# ---------------------------------------------------------------------------
# WebhookConfig validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWebhookConfig:
    """Webhook URL validation and defaults."""

    def test_valid_https_url(self) -> None:
        wh = WebhookConfig(name="test", url="https://hooks.example.com/abc")
        assert wh.url == "https://hooks.example.com/abc"

    def test_valid_http_url(self) -> None:
        wh = WebhookConfig(name="test", url="http://internal.example.com/hook")
        assert wh.url == "http://internal.example.com/hook"

    def test_invalid_url_raises(self) -> None:
        with pytest.raises(ValueError, match="must start with http"):
            WebhookConfig(name="test", url="ftp://bad.example.com")

    def test_default_format(self) -> None:
        wh = WebhookConfig(name="test", url="https://hooks.example.com/abc")
        assert wh.format == "generic"


# ---------------------------------------------------------------------------
# load_notification_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadNotificationConfig:
    """Loading notification config from schedules.yml."""

    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_notification_config(tmp_path) is None

    def test_no_notifications_section(self, tmp_path: Path) -> None:
        d = tmp_path / ".dango"
        d.mkdir()
        (d / "schedules.yml").write_text(yaml.safe_dump({"schedules": []}))
        assert load_notification_config(tmp_path) is None

    def test_valid_config(self, tmp_path: Path) -> None:
        root = _write_schedules(
            tmp_path,
            {
                "webhooks": [{"name": "slack", "url": "https://hooks.slack.com/test"}],
                "on_failure": True,
                "on_success": True,
            },
        )
        config = load_notification_config(root)
        assert config is not None
        assert len(config.webhooks) == 1
        assert config.webhooks[0].name == "slack"
        assert config.on_success is True

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        d = tmp_path / ".dango"
        d.mkdir()
        (d / "schedules.yml").write_text("{{invalid yaml")
        assert load_notification_config(tmp_path) is None


# ---------------------------------------------------------------------------
# WebhookSender
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWebhookSender:
    """Webhook delivery, retry logic, and error handling."""

    def _run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def test_unconfigured_noop(self) -> None:
        sender = WebhookSender(None)
        assert sender.is_configured is False
        # Should return without error
        self._run(
            sender.send(
                event_type=EventType.SYNC_FAILED,
                schedule_name="daily",
            )
        )

    def test_empty_webhooks_not_configured(self) -> None:
        config = NotificationConfig(webhooks=[])
        sender = WebhookSender(config)
        assert sender.is_configured is False

    def test_filtered_event_skipped(self) -> None:
        config = _make_config(on_success=False)
        sender = WebhookSender(config)
        # Sync completed with on_success=False should be skipped
        with patch(_HTTPX_CLIENT) as mock_cls:
            self._run(
                sender.send(
                    event_type=EventType.SYNC_COMPLETED,
                    schedule_name="daily",
                )
            )
            mock_cls.assert_not_called()

    def test_successful_delivery(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client(_resp(200))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                    sources=["google_analytics"],
                    error="Connection timeout",
                )
            )
            mock_client.post.assert_called_once()
            call_kwargs = mock_client.post.call_args
            payload = call_kwargs.kwargs["json"]
            assert payload["event"] == "sync_failed"
            assert payload["schedule"] == "daily"
            assert payload["sources"] == ["google_analytics"]
            assert payload["error"] == "Connection timeout"

    def test_retry_on_server_error(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client()
        mock_client.post = AsyncMock(
            side_effect=[_resp(502), _resp(200)],
        )

        with (
            patch(_HTTPX_CLIENT, return_value=mock_client),
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
        ):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            assert mock_client.post.call_count == 2

    def test_max_retries_exhausted(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client()
        mock_client.post = AsyncMock(
            side_effect=[_resp(500), _resp(500), _resp(500)],
        )

        with (
            patch(_HTTPX_CLIENT, return_value=mock_client),
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
        ):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            assert mock_client.post.call_count == 3

    def test_timeout_retries(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client()
        mock_client.post = AsyncMock(
            side_effect=[httpx.TimeoutException("timed out"), _resp(200)],
        )

        with (
            patch(_HTTPX_CLIENT, return_value=mock_client),
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
        ):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            assert mock_client.post.call_count == 2

    def test_client_error_no_retry(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client(_resp(400))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            # 4xx should not retry
            mock_client.post.assert_called_once()

    def test_connection_error_retries(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client()
        mock_client.post = AsyncMock(
            side_effect=[httpx.ConnectError("connection refused"), _resp(200)],
        )

        with (
            patch(_HTTPX_CLIENT, return_value=mock_client),
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
        ):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            assert mock_client.post.call_count == 2

    def test_send_never_raises(self) -> None:
        config = _make_config(on_failure=True)
        sender = WebhookSender(config)

        # Patch _build_json_payload to raise — send() must catch it
        with patch.object(WebhookSender, "_build_json_payload", side_effect=RuntimeError("boom")):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            # No exception should propagate

    def test_multiple_webhooks_concurrent(self) -> None:
        config = NotificationConfig(
            webhooks=[
                WebhookConfig(name="hook-a", url="https://a.example.com/hook"),
                WebhookConfig(name="hook-b", url="https://b.example.com/hook"),
            ],
            on_failure=True,
        )
        sender = WebhookSender(config)
        mock_client = _mock_httpx_client(_resp(200))

        with patch(_HTTPX_CLIENT, return_value=mock_client):
            self._run(
                sender.send(
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )
            )
            # Both webhooks should be called
            assert mock_client.post.call_count == 2
