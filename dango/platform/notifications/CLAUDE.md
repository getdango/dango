# notifications/

## Purpose

Webhook notification infrastructure for sync event notifications. Sends configurable HTTP webhooks on sync completion, failure, staleness, and retry events. Supports Slack Block Kit formatting and per-schedule notification overrides.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` (28 lines) | Re-exports public API from `webhook.py` | `WebhookSender`, `WebhookConfig`, `NotificationConfig`, `EventType`, `EventCategory`, `WebhookPayload`, `load_notification_config`, `should_notify` |
| `webhook.py` (365 lines) | Event types, config models, event filtering, async sender with retry | `WebhookSender`, `WebhookConfig`, `NotificationConfig`, `EventType`, `EventCategory`, `WebhookPayload`, `EVENT_TO_CATEGORY`, `load_notification_config`, `should_notify` |
| `slack.py` (124 lines) | Slack Block Kit formatter for webhook payloads | `format_slack_message` |

## Key Conventions

- **"Never raises" contract:** `WebhookSender.send()` wraps its full body in `try/except Exception` with `logger.warning`. Notification failures must never block the sync pipeline. Setup code before the guarded section can still propagate — ensure the entire function body is wrapped.
- **HTTP retry baseline:** Use `_RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.ConnectError)` as the retry tuple. Connection errors (reset, DNS blip) are the most common retry-worthy failures. Don't limit retries to only 5xx status codes + timeout.
- **Event filtering with per-schedule overrides:** `WebhookSender.send()` checks `on_success` (default `false`), `on_failure` (default `true`), and `on_stale` (default `true`) at the global config level. Per-schedule `notify` blocks can override these defaults.
- **Lazy imports only for formatter modules:** Don't add eager imports to `__init__.py` for `slack.py` or future format plugins. `webhook.py`'s `_format_payload()` dispatches lazily based on the `format` config field. Future format plugins (Teams, Discord) follow the same pattern.

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new event type | `webhook.py` (`EventType` enum + `EVENT_TO_CATEGORY` mapping) | `pytest tests/unit/test_webhook.py` |
| Add a new notification format | Create `{format}.py` with `format_{format}_message()`, add dispatch in `webhook.py` `_format_payload()` | `pytest tests/unit/test_webhook.py` |
| Change retry behavior | `webhook.py` (`_RETRYABLE_ERRORS`, `_MAX_RETRIES`, `_RETRY_DELAYS`) | `pytest tests/unit/test_webhook.py` |
| Modify event filtering logic | `webhook.py` (`should_notify()`) | `pytest tests/unit/test_webhook.py` |
| Update Slack message layout | `slack.py` | `pytest tests/unit/test_slack_formatter.py` |

## Dependencies

**Imports from:**
- `httpx` — async HTTP client for webhook delivery
- `yaml` — load notification config from `schedules.yml`
- `pydantic` — config models (`WebhookConfig`, `NotificationConfig`)
- `dango.logging` — `get_logger`

**Used by:**
- `dango.platform.scheduling.jobs` — sends notifications on sync completion/failure
- `dango.web.routes.schedules` — notification config/test endpoints
- `dango.config.schedules` — notification config validation

## Testing

- **Unit:** `pytest tests/unit/test_webhook.py tests/unit/test_slack_formatter.py`
- **Manual:** Configure webhook in `schedules.yml`, trigger sync, observe delivery

## Don't Modify

| File | Reason |
|------|--------|
| `EventType` enum values | Persisted in notification configs and webhook payloads |
| `WebhookPayload` field names | External webhook receivers depend on the JSON structure |
| `__init__.py` re-exports | Other modules import notification types from the package |
