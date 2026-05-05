# Sync Semantics

How data synchronization works in Dango — sync modes, lookback windows, date filtering, database source behavior, and missed schedule recovery.

## Sync Modes

Dango uses dlt's `write_disposition` to control how data is written to DuckDB. Each source has a default mode, but you can force a full refresh on any source.

### Incremental Append (`append`)

New rows are appended to the table. Existing rows are never modified or deleted. Best for event/log data where records are immutable (e.g., analytics events, ad impressions).

### Incremental Merge (`merge`)

Rows are upserted by primary key — new records are inserted, existing records are updated. Best for entity data that changes over time (e.g., CRM contacts, product listings).

### Full Refresh (`replace`)

The entire table is dropped and reloaded from scratch on every sync. Best for small datasets or sources without reliable cursors (e.g., spreadsheets, CSV files).

### Forcing a Full Refresh

Any source can be forced to do a full refresh regardless of its default mode:

```bash
dango sync --source my_source --full-refresh
```

This drops existing data and reloads everything. Useful for recovering from data quality issues or after schema changes in the source.

## Per-Source Defaults

Each source type has a default sync mode based on its data characteristics:

| Source Type | Default Mode | Notes |
|-------------|-------------|-------|
| `rest_api` | merge | Default `primary_key: "id"` per endpoint |
| `google_sheets` | replace | Re-reads entire spreadsheet |
| `google_analytics` | append | Historical report data, immutable |
| `hubspot` | merge | CRM entities upserted by ID |
| `salesforce` | merge | CRM entities upserted by ID |
| `pipedrive` | merge | CRM entities upserted by ID |
| `freshdesk` | merge | Support tickets upserted by ID |
| `zendesk` | merge | Support tickets upserted by ID |
| `stripe` | merge | Incremental with cursor |
| `shopify` | merge | E-commerce entities upserted by ID |
| `postgres` | replace | Full table snapshot (no CDC) |
| `sql_database` | replace | Full table snapshot (no CDC) |
| `mongodb` | replace | Full collection snapshot (no CDC); configurable via dlt config |
| `csv` | replace | Re-reads files fully |
| `local_files` | replace | Re-reads files fully |
| `facebook_ads` | replace | Insights without reliable cursors |
| `slack` | append | Switches to merge if `end_date` is provided |
| `github` | replace | Full reload of issues/PRs |
| `jira` | merge | Issues upserted by ID |
| `asana` | merge | Tasks upserted by ID |
| `workable` | merge | Candidates upserted by ID |
| `airtable` | merge | Records upserted by ID |
| `notion` | merge | Pages upserted by ID |

Sources not listed here default to `merge` with `primary_key: "id"`.

## Lookback Behavior

Lookback windows catch late-arriving records by shifting the sync start date backward on incremental syncs.

### Configuration

Set `lookback_days` on a source in `sources.yml`:

```yaml
sources:
  - name: stripe_data
    type: stripe
    lookback_days: 7
```

### How It Works

On each incremental sync:

1. If `lookback_days` is set, AND
2. The sync is NOT a full refresh (`--full-refresh`), AND
3. No explicit `--since` date override is provided

Then: `start_date = today - lookback_days` (ISO format, e.g., `2024-01-15`)

This means data from the last N days is re-fetched and merged, catching any records that arrived late or were updated after initial ingestion.

### When to Use

- **Stripe:** Payment disputes can be updated days after creation
- **CRM systems:** Records may be backdated or bulk-imported
- **Ad platforms:** Attribution data updates for 7-28 days after the event

If `lookback_days` is not set (default), incremental syncs resume from where they last left off using dlt's state tracking.

## Date Range Filtering

Some sources support explicit date boundaries to limit the data window.

### Supported Sources

Date filtering (`--since` / `--until`) works with sources that accept date parameters:

- Stripe (`start_date`, `end_date`)
- Facebook Ads (`start_date`)
- Google Analytics (relative dates like `"90daysAgo"`)
- Slack (`start_date`)
- Workable (`start_date`)

### CLI Usage

```bash
# Sync only data from a specific date range
dango sync --source stripe_data --since 2024-01-01 --until 2024-03-31

# Sync from a start date to present
dango sync --source stripe_data --since 2024-06-01

# Backfill a duration (alternative to --since/--until)
dango sync --source stripe_data --backfill 30d
```

The `--backfill` flag accepts durations like `7d`, `2w`, or `1m` and conflicts with `--since`/`--until` — use one or the other.

### Relative Date Strings

Google Analytics accepts relative date strings natively (e.g., `"90daysAgo"`). For other sources, dates must be in ISO format (`YYYY-MM-DD`).

### Interaction with Lookback

If `--since` or `--backfill` is provided on the command line, it takes precedence over `lookback_days`. The lookback calculation is skipped entirely.

## Database Sources

Database sources (PostgreSQL, MongoDB, SQL Database) behave differently from API sources.

### No Change Data Capture

Dango does **not** perform CDC (Change Data Capture). Database syncs are pure snapshot loads:

- The entire table (or selected tables) is read on each sync
- There is no log-based replication (no WAL reading, no oplog tailing)
- The default mode is `replace` — full table reload each time

### Deletions Are Not Propagated

If a row is deleted in the source database, it is **not** automatically removed from DuckDB. Since database sources default to `replace` mode, deletions are captured on every sync because the entire table is reloaded. However, if you switch a database source to `merge` mode (cursor-based incremental), deleted rows will persist in DuckDB indefinitely.

**Workarounds for merge mode:**

- Run `--full-refresh` periodically to reload clean data
- Build dbt models that detect stale records (e.g., flag rows missing from the latest sync)
- Use SCD Type 2 deduplication to track when records disappear

### Table Selection

Limit which tables are synced to avoid loading unnecessary data:

```yaml
sources:
  - name: app_db
    type: postgres
    config:
      table_names:
        - users
        - orders
        - products
```

For MongoDB:

```yaml
sources:
  - name: analytics_db
    type: mongodb
    config:
      collection_names:
        - events
        - sessions
```

### What "Incremental" Means for Databases

If a database source is configured with `merge` mode, it uses cursor-based state tracking (e.g., "fetch rows where `updated_at` > last sync timestamp"). This is **not** log-based replication — it still queries the source database directly and cannot detect deletes.

## Deduplication and Change Tracking

Dango auto-generates dbt staging models with deduplication logic based on source type and column analysis. This includes SCD Type 2 (Slowly Changing Dimension) support for tracking historical changes.

### How It Works

When Dango generates dbt staging models (`dango transform generate`), it inspects each table's columns and auto-infers the best deduplication strategy:

- Tables with `id` + `updated_at` columns get `last_modified` (keep most recent by timestamp)
- Other tables may get no deduplication if no suitable columns are found

The strategy is applied as SQL logic in the generated staging model — these are standard dbt models, not dbt snapshot materializations.

### Available Deduplication Strategies

| Strategy | Description |
|----------|-------------|
| `last_modified` | Keep most recent record based on timestamp |
| `first_seen` | Keep oldest record (first occurrence) |
| `composite_key` | Deduplicate using multiple columns as composite key |
| `row_number` | Keep first row when sorted by specified columns |
| `scd_type2` | Track historical changes (Slowly Changing Dimension Type 2) |

### SCD Type 2 Change Tracking

The `scd_type2` strategy generates staging models that track when each record version was valid:

- `dbt_valid_from` — when this version of the record appeared
- `dbt_valid_to` — when it was superseded (`NULL` for current records)
- Historical changes are preserved as separate rows

Best for database sources where you need change history but the source only provides current state. Combine with regular syncs — each sync captures the current snapshot, and the SCD Type 2 model tracks what changed between snapshots.

### Google Sheets Deduplication

Google Sheets sources have an explicit `deduplication` config field since sheets are re-read fully on each sync:

```yaml
sources:
  - name: my_sheets
    type: google_sheets
    google_sheets:
      spreadsheet_url_or_id: "1ABC..."
      range_names:
        - Sheet1
      deduplication: latest_only
```

Valid values: `none`, `latest_only` (default), `append_only`, `scd_type2`.

## Missed Schedules

When the Dango server is stopped (maintenance, restart, laptop sleep), scheduled syncs are missed. Dango handles this gracefully on restart.

### Default Behavior

- **`misfire_grace_time: None`** — missed jobs always run on startup, regardless of how long the server was down
- **`coalesce: True`** — if multiple runs were missed, they collapse into a single execution
- **`max_instances: 1`** — prevents overlapping runs (required by DuckDB's single-writer constraint)

This means: if a source is scheduled every hour and the server is down for 8 hours, only **one** sync runs on restart (not eight).

### dbt Coalescing

When multiple syncs finish close together, dbt runs are coalesced to avoid redundant transformations:

1. Each sync completes and registers its source as pending for dbt
2. A coalescing window (default: 10 seconds) allows additional syncs to finish
3. After the window, a single dbt run processes all pending sources together

This is configured via `dbt_coalesce_seconds` in `project.yml`:

```yaml
platform:
  dbt_coalesce_seconds: 10
```

### Per-Schedule Override

Override the default misfire behavior for individual schedules in `schedules.yml`:

```yaml
schedules:
  - name: hourly_crm_sync
    cron: "0 * * * *"
    sources:
      - hubspot_data
    misfire_grace_time: 3600  # Only run if missed within the last hour (seconds)
```

Setting `misfire_grace_time` to an integer limits how old a missed run can be before it's skipped entirely. Set to `null` (or omit) to always recover missed runs.

### Recovery Sequence on Startup

1. Server starts and initializes the scheduler
2. Scheduler detects missed jobs from SQLite job store
3. Missed jobs are coalesced (multiple missed → single execution per schedule)
4. Jobs execute (DuckDB's single-writer lock serializes concurrent write attempts)
5. After syncs complete, a coalesced dbt run processes all affected sources
