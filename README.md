# Dango

[![PyPI version](https://img.shields.io/pypi/v/getdango)](https://pypi.org/project/getdango/)
[![Python versions](https://img.shields.io/pypi/pyversions/getdango)](https://pypi.org/project/getdango/)
[![License](https://img.shields.io/github/license/getdango/dango)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/getdango/dango)](https://github.com/getdango/dango)

**Open-source data platform for small teams.**

Dango gives you a complete data stack — ingestion, warehouse, transformations, and dashboards — with the operational machinery a self-assembled stack doesn't have: sync queue management, lock recovery, empty-replace protection, schema drift detection, credential health checks, and backups. It combines [dlt](https://dlthub.com/) for data loading, [DuckDB](https://duckdb.org/) as the analytics database, [dbt](https://www.getdbt.com/) for SQL transformations, and [Metabase](https://www.metabase.com/) for dashboards. One install, one command to start.

> **Upgrading from v0.1.x?** v1.0.0 is a complete rewrite. Back up your data and run `dango init` to create a new v1 project. See the [migration guide](https://docs.getdango.dev) for details.

<img src="https://raw.githubusercontent.com/getdango/dango/main/docs/assets/demo/dango-demo-v1.gif" alt="Dango demo: install with one command, add and sync a data source, then tour the web UI and chart the data in Metabase (148 seconds)" width="800">

## Quick Start

**Prerequisites:** Python 3.11-3.13, [Docker](https://docs.docker.com/desktop/) (for Metabase and dbt docs)

**macOS / Linux:**

```bash
curl -sSL https://getdango.dev/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://getdango.dev/install.ps1 | iex
```

**Prefer pip?**

```bash
mkdir my-project && cd my-project
pip install getdango
dango init
dango start
```

Open [http://localhost:8800](http://localhost:8800) to see your data platform.

For detailed installation instructions, see the [documentation](https://docs.getdango.dev).

## Use Dango with your coding agent

```bash
dango mcp setup
```

Connects Claude Code, Cursor, or Windsurf to your project over MCP — your agent can set up sources
(credentials stay in your `.env`, never in the chat), sync, build and document dbt models, manage
schedules, check PII findings and schema drift, diagnose failures, and run read-only queries with
PII columns masked. Remote tools default to a dry run. See the
[full guide](https://docs.getdango.dev/guides/mcp-claude-code/) for the complete tool reference
and worked examples.

## Features

- **34 data sources** — Stripe, Google Sheets, Google Analytics, Shopify, PostgreSQL, MySQL, CSV, REST APIs, and more
- **Auto-generated dbt models** — staging models created automatically when you add a source
- **Data catalog** — browse tables, columns, and profiling stats; view dbt lineage and test results
- **Web dashboard** — monitor syncs, manage sources, and view platform health
- **Metabase integration** — dashboards and SQL queries, auto-configured and ready to use
- **Scripts** — schedule custom Python scripts alongside your syncs; runs on a schedule with access to your warehouse
- **Cloud deployment** — deploy to DigitalOcean or any server with `dango deploy`
- **Authentication** — admin login, user management, 2FA, API keys
- **Schema drift detection** — get alerted when source schemas change
- **PII scanning** — detect personally identifiable information across your tables
- **Notebooks** — Marimo notebooks connected to your DuckDB warehouse
- **Monitoring** — metric tracking with trend detection and drill-downs
- **Scheduled syncs** — cron-based scheduling with queued execution, retry, and timeout handling
- **Webhooks** — Slack notifications for sync results and alerts
- **Local backup** — `dango backup` lists and restores local project backups
- **File watcher** — auto-sync when CSV files change on disk

## Screenshots

<img src="https://raw.githubusercontent.com/getdango/dango/main/docs/assets/screenshots/sources.png" alt="Dango Sources page showing connected data sources with sync status" width="800">

*Data Sources page — monitor syncs, row counts, and schedule status for all your data sources.*

<img src="https://raw.githubusercontent.com/getdango/dango/main/docs/assets/screenshots/catalog.png" alt="Dango Data Catalog showing columns, lineage, and profiling" width="800">

*Data Catalog — browse tables, explore columns with profiling stats, view data lineage.*

<img src="https://raw.githubusercontent.com/getdango/dango/main/docs/assets/screenshots/models.png" alt="Dango dbt Models page" width="800">

*dbt Models — view model status, run transformations, and track test results.*

## Architecture

```
Sources  →  dlt  →  DuckDB  →  dbt  →  Metabase
(APIs,       (load)  (warehouse) (transform) (dashboards)
 CSVs,
 databases)
```

Data lives in a DuckDB file on the machine that runs Dango; no external warehouse is needed. With
`dango deploy`, that machine is your server, and its data and credentials live there. Server backups
go to DigitalOcean Spaces only if you enable them. Telemetry is described [below](#telemetry).

## Tech Stack

| Component | Tool | Role |
|-----------|------|------|
| Ingestion | [dlt](https://dlthub.com/) | Load data from 34 sources |
| Warehouse | [DuckDB](https://duckdb.org/) | Embedded analytics database |
| Transformation | [dbt](https://www.getdbt.com/) | SQL modeling and testing |
| Dashboards | [Metabase](https://www.metabase.com/) | BI and SQL queries |
| Web UI | [FastAPI](https://fastapi.tiangolo.com/) | Monitoring and management |
| Containers | [Docker](https://www.docker.com/) | Metabase and service orchestration |

## Documentation

Full documentation at [docs.getdango.dev](https://docs.getdango.dev).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

## Telemetry

`dango init` asks once whether to allow anonymous usage data from Dango (install ping and
periodic heartbeat: version, OS, configured source types; never source names, credentials or
data) and from the tools it uses: dbt, dlt and Metabase each report their own anonymous
telemetry. One answer covers all four. To opt out later (or in), run
`dango telemetry off --all` (or `on --all`); `dango telemetry status` shows each provider.
Every outbound host is listed in [`docs/network-egress.yml`](docs/network-egress.yml).

## Security and limitations

**Roles.** Dango has three roles: Admin, Editor and Viewer. Viewers read dashboards and status.
**Editors can run Python (notebooks and scripts) as the server user and can run SQL over every
table**, so treat Editor as a trusted, code-capable role. Dango has no table- or row-level
entitlements (DuckDB has no users or grants), so anyone who can query can query everything.

- Query results in the web UI are not PII-masked. The MCP `query` tool masks PII, by output column
  name only.
- Code in the shared Marimo notebook process cannot be attributed to a single user.
- A cloud deploy without a domain serves HTTP only: logins and cookies are unencrypted. Set a domain
  for HTTPS.
- Only restore backups you made yourself; an archive can contain files that later run as code.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

### MCP tools by blast radius

An MCP client acts with the permissions of whoever runs `dango mcp`. The warehouse and your sources
hold untrusted text, so a prompt-injected agent can call any tool you allow.

| Tool | What it can change | Guard |
|------|--------------------|-------|
| Read-only: `list_*`, `get_*`, `validate_*`, `docs_coverage`, `remote_status`, `remote_logs`, `remote_history` | Nothing | Log output is redacted |
| `query`, `remote_query` | Nothing; reads the warehouse | One SELECT only; file and URL access blocked; PII masked by column name; row cap |
| `create_model`, `update_model`, `remove_model` | What dbt runs against the warehouse | Model SQL validated: no file or URL access, no `COPY`/`ATTACH`/`INSTALL`/`LOAD`, restricted Jinja |
| `create_source`, `update_source`, `set_source_enabled`, `remove_source` | Which sources sync and where | Secrets are refused as config values and stay in `.env`; `rest_api` is edited only with `dango source edit` |
| `run_sync`, `run_transform`, `run_doctor`, `generate_docs`, `scan_pii` | API quota, load, warehouse contents | `run_sync` accepts `dry_run` |
| `accept_schema_drift`, `set_pii_override`, `update_source_table_docs`, `add_schedule`, `update_schedule`, `set_schedule_enabled`, `remove_schedule`, `reload_schedules` | Local project configuration | None beyond your client's approval prompt |
| `remote_sync` | Runs a real sync on your server | Needs a deployment; no dry run |
| `remote_push` | Replaces models, scripts and sources on your server | Dry run by default; needs `confirm=True` to apply |

**Do not auto-approve mutation tools; an agent with them can change what runs against your warehouse.**

## Known limitations

Dango is honest about what it doesn't do yet. The short version — full detail at
[docs.getdango.dev/reference/limitations](https://docs.getdango.dev/reference/limitations):

- Single-writer concurrency until the Quack migration lands (DuckDB 2.0)
- No streaming, CDC, or reverse ETL
- 34 sources today, added on demand
- BYOS deployments need to configure their own backup destination (warned at deploy time)

## Links

- [PyPI](https://pypi.org/project/getdango/)
- [Changelog](CHANGELOG.md)
- [Issues](https://github.com/getdango/dango/issues)
