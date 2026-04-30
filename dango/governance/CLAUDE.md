# governance/

## Purpose

Data governance module: schema drift detection and PII scanning. Monitors DuckDB warehouse schemas for changes between syncs, records drift events, scans for PII in string columns, and alerts via webhooks.

## Files

| File | Purpose | Key Functions/Classes |
|------|---------|----------------------|
| `__init__.py` | Re-exports public API | `DriftEvent`, `DriftResponse`, `PiiFinding`, `PiiOverride`, `PiiOverrideRequest`, `PiiOverridesResponse`, `PiiResponse`, `detect_drift_for_sources`, `detect_table_drift`, `get_drift_history`, `scan_sources_for_pii`, `scan_table_for_pii`, `get_pii_findings`, `get_pii_overrides`, `set_pii_override`, `delete_pii_override` |
| `models.py` (~93 lines) | Pydantic V2 response models | `DriftEvent`, `DriftResponse`, `PiiFinding`, `PiiResponse`, `PiiOverride`, `PiiOverrideRequest`, `PiiOverridesResponse` |
| `schema_drift.py` (~490 lines) | Schema drift detection engine | `detect_drift_for_sources()`, `detect_table_drift()`, `get_drift_history()`, `_send_drift_webhook()` |
| `pii_detector.py` (~602 lines) | PII scanning engine (Presidio + spaCy) | `scan_sources_for_pii()`, `scan_table_for_pii()`, `get_pii_findings()`, `_send_pii_webhook()`, `_register_intl_phone_recognizer()` |
| `pii_overrides.py` (~174 lines) | PII override CRUD | `get_overrides_for_table()`, `get_pii_overrides()`, `set_pii_override()`, `delete_pii_override()` |

## Common Tasks

| To... | Modify... | Test with... |
|-------|-----------|--------------|
| Add a new drift event type | `schema_drift.py` (`detect_table_drift()` diff logic) | `pytest tests/unit/test_drift_detection.py` |
| Query drift history | `schema_drift.py` (`get_drift_history()`) | `pytest tests/unit/test_drift_detection.py` |
| Change drift webhook format | `schema_drift.py` (`_send_drift_webhook()`) | `pytest tests/unit/test_drift_detection.py` |
| Add drift CLI output columns | `dango/cli/commands/governance.py` | `dango governance drift-report` |
| View drift via API | `dango/web/routes/governance.py` | `GET /api/governance/schema-drift` |
| Add PII entity types | `pii_detector.py` (`SCAN_ENTITIES` constant) | `pytest tests/unit/test_pii_detection.py` |
| Change PII scan threshold | `pii_detector.py` (`SCORE_THRESHOLD` constant) | `pytest tests/unit/test_pii_detection.py` |
| Query PII findings | `pii_detector.py` (`get_pii_findings()`) | `pytest tests/unit/test_pii_detection.py` |
| Add PII CLI output columns | `dango/cli/commands/governance.py` | `dango governance pii-report` |
| View PII via API | `dango/web/routes/governance.py` | `GET /api/governance/pii` |
| Set/delete PII override | `pii_overrides.py` | `pytest tests/unit/test_pii_overrides.py` |
| List PII overrides via CLI | `dango/cli/commands/governance.py` | `dango governance pii-list` |
| Set PII override via CLI | `dango/cli/commands/governance.py` | `dango governance pii-set SOURCE TABLE COL --status pii` |
| View PII overrides via API | `dango/web/routes/governance.py` | `GET /api/governance/pii/overrides` |

## Dependencies

**Imports from:**
- `dango.utils.dango_db` — `connect()` context manager for SQLite
- `dango.validation` — `validate_identifier()`, `validate_source_name()`
- `dango.logging` — `get_logger()`
- `dango.platform.notifications.webhook` — `EventType`, `WebhookPayload`, `load_notification_config`, `should_notify`
- `dango.platform.notifications.slack` — `format_slack_message()`
- `duckdb` — read-only DuckDB access (lazy import)
- `httpx` — sync webhook delivery (lazy import)
- `spacy` — NLP model for Presidio (lazy import, `pii_detector.py`)
- `presidio_analyzer` — PII analysis engine (lazy import, `pii_detector.py`)

**Used by:**
- `dango/utils/post_sync.py` — `_run_drift_detection()` calls `detect_drift_for_sources()`, `_run_pii_scan()` calls `scan_sources_for_pii()`
- `dango/web/routes/governance.py` — `GET /api/governance/schema-drift` calls `get_drift_history()`, `GET /api/governance/pii` calls `get_pii_findings()`
- `dango/cli/commands/governance.py` — `dango governance drift-report` calls `get_drift_history()`, `dango governance pii-report` calls `get_pii_findings()`

## Testing

- **Unit (drift):** `pytest tests/unit/test_drift_detection.py`
- **Unit (PII):** `pytest tests/unit/test_pii_detection.py`
- **Unit (PII overrides):** `pytest tests/unit/test_pii_overrides.py`
- **Integration (drift):** `pytest tests/integration/test_drift_integration.py`
- **Integration (PII):** `pytest tests/integration/test_pii_integration.py`
- **Related:** `pytest tests/unit/test_post_sync.py tests/unit/test_webhook.py tests/unit/test_slack_formatter.py`

## Don't Modify

| Item | Reason |
|------|--------|
| `drift_events` table schema | Existing events depend on the column structure (defined in `dango/utils/dango_db.py`) |
| `schema_baselines` table schema | Baseline comparison logic depends on exact columns |
| `pii_findings` table schema | Cached findings depend on the column structure (defined in `dango/utils/dango_db.py`) |
| `DriftEvent` field names | Web API consumers depend on the response shape |
| `PiiFinding` field names | Web API consumers depend on the response shape |
| `pii_overrides` table schema | Override CRUD depends on the column structure (defined in `dango/utils/dango_db.py`) |
| `PiiOverride` field names | Web API consumers depend on the response shape |
