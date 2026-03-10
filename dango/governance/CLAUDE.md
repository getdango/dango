# governance/

## Purpose

Data governance module: schema drift detection and PII scanning. Monitors DuckDB warehouse schemas for changes between syncs, records drift events, and alerts via webhooks.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Re-exports public API | `DriftEvent`, `DriftResponse`, `detect_drift_for_sources`, `detect_table_drift`, `get_drift_history` |
| `models.py` (~35 lines) | Pydantic V2 response models | `DriftEvent`, `DriftResponse` |
| `schema_drift.py` (~280 lines) | Schema drift detection engine | `detect_drift_for_sources()`, `detect_table_drift()`, `get_drift_history()`, `_send_drift_webhook()` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new drift event type | `schema_drift.py` (`detect_table_drift()` diff logic) | `pytest tests/unit/test_drift_detection.py` |
| Query drift history | `schema_drift.py` (`get_drift_history()`) | `pytest tests/unit/test_drift_detection.py` |
| Change webhook notification format | `schema_drift.py` (`_send_drift_webhook()`) | `pytest tests/unit/test_drift_detection.py` |
| Add drift CLI output columns | `dango/cli/commands/governance.py` | `dango governance drift-report` |
| View drift via API | `dango/web/routes/governance.py` | `GET /api/governance/schema-drift` |

## Dependencies

**Imports from:**
- `dango.utils.dango_db` — `connect()` context manager for SQLite
- `dango.validation` — `validate_identifier()`, `validate_source_name()`
- `dango.logging` — `get_logger()`
- `dango.platform.notifications.webhook` — `EventType`, `WebhookPayload`, `load_notification_config`, `should_notify`
- `dango.platform.notifications.slack` — `format_slack_message()`
- `duckdb` — read-only DuckDB access (lazy import)
- `httpx` — sync webhook delivery (lazy import)

**Used by:**
- `dango/utils/post_sync.py` — `_run_drift_detection()` calls `detect_drift_for_sources()`
- `dango/web/routes/governance.py` — `GET /api/governance/schema-drift` calls `get_drift_history()`
- `dango/cli/commands/governance.py` — `dango governance drift-report` calls `get_drift_history()`

## Testing

- **Unit:** `pytest tests/unit/test_drift_detection.py`
- **Integration:** `pytest tests/integration/test_drift_integration.py`
- **Related:** `pytest tests/unit/test_post_sync.py tests/unit/test_webhook.py tests/unit/test_slack_formatter.py`

## Don't Modify

| Item | Reason |
|------|--------|
| `drift_events` table schema | Existing events depend on the column structure (defined in `dango/utils/dango_db.py`) |
| `schema_baselines` table schema | Baseline comparison logic depends on exact columns |
| `DriftEvent` field names | Web API consumers depend on the response shape |
