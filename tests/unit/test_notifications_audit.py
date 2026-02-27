"""tests/unit/test_notifications_audit.py

TEST-009 coverage audit: gap-fill tests for webhook, Slack formatter,
and freshness timezone edge cases not covered by TASK-041/043/044.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from dango.platform.notifications.slack import format_slack_message
from dango.platform.notifications.webhook import (
    _TIMEOUT,
    EventType,
    NotificationConfig,
    WebhookConfig,
    WebhookPayload,
    WebhookSender,
)

_JOBS_MOD = "dango.platform.scheduling.jobs"
_NOTIF_MOD = "dango.platform.notifications.webhook"
_HIST_MOD = "dango.utils.sync_history"
_HTTPX_CLIENT = "dango.platform.notifications.webhook.httpx.AsyncClient"
_ASYNC_SLEEP = "dango.platform.notifications.webhook.asyncio.sleep"


def _make_config(**overrides):
    """Build a NotificationConfig with sensible defaults."""
    defaults = {
        "webhooks": [
            {"name": "test-hook", "url": "https://example.com/hook"},
        ],
        "on_failure": True,
        "on_success": False,
        "on_stale": True,
    }
    defaults.update(overrides)
    return NotificationConfig(**defaults)


def _mock_httpx_client(response=None):
    """Create a mock httpx.AsyncClient context manager."""
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


def _resp(status=200):
    """Build a mock httpx response with the given status code."""
    r = MagicMock()
    r.status_code = status
    return r


# ---------------------------------------------------------------------------
# Webhook timeout contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWebhookTimeout:
    """Verify webhook timeout constant and fire-and-forget behavior."""

    def test_timeout_constant_is_10_seconds(self):
        """_TIMEOUT documents the 10-second contract."""
        assert _TIMEOUT == 10.0

    def test_notify_fire_and_forget(self):
        """_notify() schedules the coroutine but does NOT await the future."""
        import dango.platform.scheduling.jobs as jobs_mod

        loop = MagicMock(spec=asyncio.AbstractEventLoop)
        future = MagicMock()
        old = jobs_mod._event_loop
        try:
            jobs_mod._event_loop = loop

            sender = MagicMock()
            sender.is_configured = True
            sender.send = MagicMock(return_value="fake_coro")

            with patch(f"{_JOBS_MOD}.asyncio") as mock_asyncio:
                mock_asyncio.run_coroutine_threadsafe.return_value = future

                jobs_mod._notify(
                    sender,
                    event_type=EventType.SYNC_FAILED,
                    schedule_name="daily",
                )

            # Coroutine was scheduled
            mock_asyncio.run_coroutine_threadsafe.assert_called_once()
            # Fire-and-forget: .result() must NOT be called on the future
            future.result.assert_not_called()
        finally:
            jobs_mod._event_loop = old


# ---------------------------------------------------------------------------
# Malformed URL validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMalformedUrl:
    """Additional URL validation edge cases beyond ftp://."""

    def test_empty_url_rejected(self):
        with pytest.raises(ValidationError):
            WebhookConfig(name="x", url="")

    def test_whitespace_url_rejected(self):
        with pytest.raises(ValidationError):
            WebhookConfig(name="x", url="   ")

    def test_no_scheme_url_rejected(self):
        with pytest.raises(ValidationError):
            WebhookConfig(name="x", url="example.com/hook")


# ---------------------------------------------------------------------------
# Concurrent notifications
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConcurrentNotifications:
    """Multiple independent senders delivering concurrently."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_multiple_senders_all_deliver(self):
        """Three independent WebhookSender instances all POST."""
        senders = []
        for i in range(3):
            config = NotificationConfig(
                webhooks=[
                    WebhookConfig(name=f"hook-{i}", url=f"https://h{i}.example.com/hook"),
                ],
                on_failure=True,
            )
            senders.append(WebhookSender(config))

        mock_client = _mock_httpx_client(_resp(200))

        with patch(_HTTPX_CLIENT, return_value=mock_client):

            async def _send_all():
                await asyncio.gather(
                    *(
                        s.send(event_type=EventType.SYNC_FAILED, schedule_name="daily")
                        for s in senders
                    )
                )

            self._run(_send_all())

        assert mock_client.post.call_count == 3

    def test_one_failure_doesnt_block_others(self):
        """One sender raising on all retries doesn't prevent others from delivering."""
        configs = []
        for i in range(3):
            configs.append(
                NotificationConfig(
                    webhooks=[
                        WebhookConfig(name=f"hook-{i}", url=f"https://h{i}.example.com/hook"),
                    ],
                    on_failure=True,
                )
            )
        senders = [WebhookSender(c) for c in configs]

        # Track which URLs were POSTed to
        posted_urls: list[str] = []

        async def _url_dispatch(url, **kwargs):
            posted_urls.append(url)
            if "h0.example.com" in url:
                return _resp(500)
            return _resp(200)

        mock_client = AsyncMock()
        mock_client.post = _url_dispatch
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch(_HTTPX_CLIENT, return_value=mock_client),
            patch(_ASYNC_SLEEP, new_callable=AsyncMock),
        ):

            async def _send_all():
                await asyncio.gather(
                    *(
                        s.send(event_type=EventType.SYNC_FAILED, schedule_name="daily")
                        for s in senders
                    )
                )

            self._run(_send_all())

        # h0 exhausted all 3 retries, h1 and h2 each succeeded on first try
        h0_calls = [u for u in posted_urls if "h0.example.com" in u]
        h1_calls = [u for u in posted_urls if "h1.example.com" in u]
        h2_calls = [u for u in posted_urls if "h2.example.com" in u]
        assert len(h0_calls) == 3  # 3 retry attempts, all 500
        assert len(h1_calls) == 1  # succeeded first try
        assert len(h2_calls) == 1  # succeeded first try


# ---------------------------------------------------------------------------
# Slack edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSlackEdgeCases:
    """Slack formatter edge cases not covered by test_slack_formatter.py."""

    def test_long_error_message_preserved(self):
        """1000-char error message is not truncated."""
        long_error = "E" * 1000
        payload = WebhookPayload(
            event_type=EventType.SYNC_FAILED,
            schedule_name="daily",
            error=long_error,
        )
        msg = format_slack_message(payload)
        body = msg["attachments"][0]["blocks"][1]["text"]["text"]
        assert long_error in body

    def test_special_characters_preserved(self):
        """Markdown special chars (*bold*, `code`, <angle>) are preserved."""
        payload = WebhookPayload(
            event_type=EventType.SYNC_FAILED,
            schedule_name="daily",
            error="*bold* `code` <angle> & ampersand",
        )
        msg = format_slack_message(payload)
        body = msg["attachments"][0]["blocks"][1]["text"]["text"]
        assert "*bold* `code` <angle> & ampersand" in body

    def test_empty_sources_on_failure(self):
        """SYNC_FAILED with sources=[] omits Sources line, includes Error."""
        payload = WebhookPayload(
            event_type=EventType.SYNC_FAILED,
            schedule_name="daily",
            sources=[],
            error="Connection refused",
        )
        msg = format_slack_message(payload)
        body = msg["attachments"][0]["blocks"][1]["text"]["text"]
        assert "Sources" not in body
        assert "Connection refused" in body


# ---------------------------------------------------------------------------
# Freshness timezone format edge cases
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFreshnessTimezones:
    """_check_freshness correctly handles various timezone suffix formats."""

    def _run_freshness(self, tmp_path, completed_at):
        """Run _check_freshness with a given completed_at string."""
        from dango.platform.scheduling.jobs import _check_freshness

        mock_sender = MagicMock()
        mock_sender.is_configured = True

        notif_config = MagicMock()
        notif_config.stale_threshold_hours = 24

        with (
            patch(f"{_NOTIF_MOD}.load_notification_config", return_value=notif_config),
            patch(
                f"{_HIST_MOD}.load_sync_history",
                return_value=[{"completed_at": completed_at}],
            ),
            patch(f"{_JOBS_MOD}._broadcast") as mock_bc,
            patch(f"{_JOBS_MOD}._notify") as mock_notify,
        ):
            _check_freshness(tmp_path, "daily", ["src1"], mock_sender)
            return mock_bc, mock_notify

    def test_stale_utc_offset_format(self, tmp_path):
        """completed_at='...+00:00' is parsed and detected as stale."""
        mock_bc, mock_notify = self._run_freshness(tmp_path, "2026-01-01T00:00:00+00:00")
        assert any(c.args[0].get("event") == "sync_stale" for c in mock_bc.call_args_list)
        mock_notify.assert_called_once()

    def test_stale_z_suffix_format(self, tmp_path):
        """completed_at='...Z' is parsed and detected as stale."""
        mock_bc, mock_notify = self._run_freshness(tmp_path, "2026-01-01T00:00:00Z")
        assert any(c.args[0].get("event") == "sync_stale" for c in mock_bc.call_args_list)
        mock_notify.assert_called_once()

    def test_stale_plus_0000_format(self, tmp_path):
        """completed_at='...+0000' is parsed and detected as stale."""
        mock_bc, mock_notify = self._run_freshness(tmp_path, "2026-01-01T00:00:00+0000")
        assert any(c.args[0].get("event") == "sync_stale" for c in mock_bc.call_args_list)
        mock_notify.assert_called_once()

    def test_stale_plain_iso_format(self, tmp_path):
        """completed_at without timezone suffix is parsed and detected as stale."""
        mock_bc, mock_notify = self._run_freshness(tmp_path, "2026-01-01T00:00:00")
        assert any(c.args[0].get("event") == "sync_stale" for c in mock_bc.call_args_list)
        mock_notify.assert_called_once()

    def test_fresh_with_z_suffix_not_stale(self, tmp_path):
        """Recent UTC timestamp with Z suffix does not trigger stale notification."""
        recent = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
        mock_bc, mock_notify = self._run_freshness(tmp_path, recent)
        mock_bc.assert_not_called()
        mock_notify.assert_not_called()
