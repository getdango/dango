"""dango/platform/notifications/slack.py

Slack Block Kit formatter for webhook notifications.

Converts ``WebhookPayload`` into Slack-compatible JSON with colored sidebar
attachments.  Each event type gets a distinct color and block layout.
"""

from __future__ import annotations

from typing import Any

from dango.platform.notifications.webhook import EventType, WebhookPayload

__all__ = ["format_slack_message"]

# Color mapping per event type (Slack attachment sidebar)
_COLORS: dict[EventType, str] = {
    EventType.SYNC_COMPLETED: "#2eb886",  # green
    EventType.SYNC_FAILED: "#e01e5a",  # red
    EventType.SYNC_STALE: "#ecb22e",  # amber
    EventType.SYNC_RETRYING: "#aaaaaa",  # gray
}

_HEADERS: dict[EventType, str] = {
    EventType.SYNC_COMPLETED: "Sync Completed",
    EventType.SYNC_FAILED: "Sync Failed",
    EventType.SYNC_STALE: "Sync Stale",
    EventType.SYNC_RETRYING: "Sync Retrying",
}


def format_slack_message(payload: WebhookPayload) -> dict[str, Any]:
    """Format a webhook payload as a Slack Block Kit message.

    Returns a dict with ``attachments`` containing a single attachment with a
    colored sidebar and Block Kit blocks for the event details.
    """
    color = _COLORS.get(payload.event_type, "#aaaaaa")
    blocks: list[dict[str, Any]] = []

    # Header
    header_text = _HEADERS.get(payload.event_type, payload.event_type.value)
    blocks.append(_header_block(header_text))

    # Body section — varies by event type
    body = _build_body(payload)
    blocks.append(_mrkdwn_section(body))

    # Optional dashboard link
    if payload.dashboard_url:
        blocks.append(_mrkdwn_section(f"<{payload.dashboard_url}|View Dashboard>"))

    return {"attachments": [{"color": color, "blocks": blocks}]}


def _build_body(payload: WebhookPayload) -> str:
    """Build the mrkdwn body text for the event type."""
    lines: list[str] = [f"*Schedule:* {payload.schedule_name}"]

    if payload.sources:
        lines.append(f"*Sources:* {', '.join(payload.sources)}")

    if payload.event_type == EventType.SYNC_COMPLETED:
        if payload.rows_loaded is not None:
            lines.append(f"*Rows loaded:* {payload.rows_loaded:,}")
        if payload.duration_seconds is not None:
            lines.append(f"*Duration:* {_format_duration(payload.duration_seconds)}")

    elif payload.event_type == EventType.SYNC_FAILED:
        if payload.error:
            lines.append(f"*Error:* {payload.error}")

    elif payload.event_type == EventType.SYNC_STALE:
        if payload.stale_hours is not None:
            lines.append(f"*Hours since last sync:* {payload.stale_hours:.1f}")

    elif payload.event_type == EventType.SYNC_RETRYING:
        if payload.attempt_number is not None:
            lines.append(f"*Attempt:* {payload.attempt_number}")
        if payload.next_retry_at is not None:
            lines.append(f"*Next retry:* {payload.next_retry_at.strftime('%H:%M:%S UTC')}")

    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string.

    Examples: ``45.2`` → ``"45s"``, ``150.5`` → ``"2m 30s"``,
    ``3661.0`` → ``"1h 1m"``.
    """
    total = int(seconds)
    if total < 60:
        return f"{total}s"

    minutes, secs = divmod(total, 60)
    if minutes < 60:
        if secs:
            return f"{minutes}m {secs}s"
        return f"{minutes}m"

    hours, mins = divmod(minutes, 60)
    if mins:
        return f"{hours}h {mins}m"
    return f"{hours}h"


def _header_block(text: str) -> dict[str, Any]:
    """Build a Slack Block Kit header block."""
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text, "emoji": True},
    }


def _mrkdwn_section(text: str) -> dict[str, Any]:
    """Build a Slack Block Kit section block with mrkdwn text."""
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }
