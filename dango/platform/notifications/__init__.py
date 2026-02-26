"""dango/platform/notifications/__init__.py

Webhook notification infrastructure for sync event notifications.
"""

from .slack import format_slack_message
from .webhook import (
    EVENT_TO_CATEGORY,
    EventCategory,
    EventType,
    NotificationConfig,
    WebhookConfig,
    WebhookPayload,
    WebhookSender,
    load_notification_config,
    should_notify,
)

__all__ = [
    "EVENT_TO_CATEGORY",
    "EventCategory",
    "EventType",
    "NotificationConfig",
    "WebhookConfig",
    "WebhookPayload",
    "WebhookSender",
    "format_slack_message",
    "load_notification_config",
    "should_notify",
]
