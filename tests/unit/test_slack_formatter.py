"""tests/unit/test_slack_formatter.py

Tests for the Slack Block Kit formatter in
dango/platform/notifications/slack.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from dango.platform.notifications.slack import (
    _format_duration,
    format_slack_message,
)
from dango.platform.notifications.webhook import EventType, WebhookPayload


def _make_payload(**overrides) -> WebhookPayload:
    """Build a WebhookPayload with sensible defaults."""
    defaults = {
        "event_type": EventType.SYNC_COMPLETED,
        "schedule_name": "daily-analytics",
        "sources": ["google_analytics", "stripe"],
    }
    defaults.update(overrides)
    return WebhookPayload(**defaults)


# ---------------------------------------------------------------------------
# format_slack_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatSlackMessage:
    """Slack Block Kit formatting for each event type."""

    def test_sync_completed_color_and_header(self):
        payload = _make_payload(
            event_type=EventType.SYNC_COMPLETED,
            rows_loaded=1500,
            duration_seconds=150.5,
        )
        msg = format_slack_message(payload)

        attachment = msg["attachments"][0]
        assert attachment["color"] == "#2eb886"
        header = attachment["blocks"][0]
        assert header["type"] == "header"
        assert header["text"]["text"] == "Sync Completed"
        assert header["text"]["emoji"] is True

    def test_sync_completed_body_fields(self):
        payload = _make_payload(
            event_type=EventType.SYNC_COMPLETED,
            rows_loaded=2500,
            duration_seconds=65.0,
        )
        msg = format_slack_message(payload)
        body_text = msg["attachments"][0]["blocks"][1]["text"]["text"]
        assert "daily-analytics" in body_text
        assert "google_analytics" in body_text
        assert "2,500" in body_text
        assert "1m 5s" in body_text

    def test_sync_failed_color_and_error(self):
        payload = _make_payload(
            event_type=EventType.SYNC_FAILED,
            error="Connection timeout after 30s",
        )
        msg = format_slack_message(payload)

        attachment = msg["attachments"][0]
        assert attachment["color"] == "#e01e5a"
        body_text = attachment["blocks"][1]["text"]["text"]
        assert "Connection timeout after 30s" in body_text

    def test_sync_stale_color_and_hours(self):
        payload = _make_payload(
            event_type=EventType.SYNC_STALE,
            stale_hours=36.5,
        )
        msg = format_slack_message(payload)

        attachment = msg["attachments"][0]
        assert attachment["color"] == "#ecb22e"
        header_text = attachment["blocks"][0]["text"]["text"]
        assert header_text == "Sync Stale"
        body_text = attachment["blocks"][1]["text"]["text"]
        assert "36.5" in body_text

    def test_sync_retrying_color_and_attempt(self):
        next_retry = datetime(2026, 2, 26, 14, 30, 0, tzinfo=timezone.utc)
        payload = _make_payload(
            event_type=EventType.SYNC_RETRYING,
            attempt_number=2,
            next_retry_at=next_retry,
        )
        msg = format_slack_message(payload)

        attachment = msg["attachments"][0]
        assert attachment["color"] == "#aaaaaa"
        body_text = attachment["blocks"][1]["text"]["text"]
        assert "Attempt:* 2" in body_text
        assert "14:30:00 UTC" in body_text

    def test_dashboard_url_included(self):
        payload = _make_payload(
            dashboard_url="https://metabase.example.com/dashboard/1",
        )
        msg = format_slack_message(payload)
        blocks = msg["attachments"][0]["blocks"]
        # Header + body + dashboard link = 3 blocks
        assert len(blocks) == 3
        link_text = blocks[2]["text"]["text"]
        assert "https://metabase.example.com/dashboard/1" in link_text
        assert "View Dashboard" in link_text

    def test_no_dashboard_url(self):
        payload = _make_payload()
        msg = format_slack_message(payload)
        blocks = msg["attachments"][0]["blocks"]
        # Header + body only = 2 blocks
        assert len(blocks) == 2

    def test_minimal_payload(self):
        payload = WebhookPayload(
            event_type=EventType.SYNC_COMPLETED,
            schedule_name="minimal",
        )
        msg = format_slack_message(payload)
        attachment = msg["attachments"][0]
        assert attachment["color"] == "#2eb886"
        body_text = attachment["blocks"][1]["text"]["text"]
        assert "minimal" in body_text
        # No sources, no rows, no duration — just schedule name
        assert "Sources" not in body_text
        assert "Rows" not in body_text


# ---------------------------------------------------------------------------
# _format_duration
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFormatDuration:
    """Human-readable duration formatting."""

    def test_seconds_only(self):
        assert _format_duration(45.2) == "45s"

    def test_zero_seconds(self):
        assert _format_duration(0.0) == "0s"

    def test_minutes_and_seconds(self):
        assert _format_duration(150.5) == "2m 30s"

    def test_exact_minutes(self):
        assert _format_duration(120.0) == "2m"

    def test_hours_and_minutes(self):
        assert _format_duration(3661.0) == "1h 1m"

    def test_exact_hours(self):
        assert _format_duration(3600.0) == "1h"

    def test_boundary_59_seconds(self):
        assert _format_duration(59.9) == "59s"

    def test_boundary_60_seconds(self):
        assert _format_duration(60.0) == "1m"
