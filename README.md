# Dango

[![PyPI version](https://img.shields.io/pypi/v/getdango)](https://pypi.org/project/getdango/)
[![Python versions](https://img.shields.io/pypi/pyversions/getdango)](https://pypi.org/project/getdango/)
[![License](https://img.shields.io/github/license/getdango/dango)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/getdango/dango)](https://github.com/getdango/dango)

**The data platform your coding agent can run.**

Dango gives you a complete data stack — ingestion, warehouse, transformations, and dashboards — with the operational machinery a self-assembled stack (or an LLM improvising one) doesn't have: sync queue management, lock recovery, empty-replace protection, schema drift detection, credential health checks, and backups. It combines [dlt](https://dlthub.com/) for data loading, [DuckDB](https://duckdb.org/) as the analytics database, [dbt](https://www.getdbt.com/) for SQL transformations, and [Metabase](https://www.metabase.com/) for dashboards. One `pip install`, one command to start.

> **Upgrading from v0.1.x?** v1.0.0 is a complete rewrite. Back up your data and run `dango init` to create a new v1 project. See the [migration guide](https://docs.getdango.dev) for details.

## Quick Start

**Prerequisites:** Python 3.10-3.13, [Docker](https://docs.docker.com/desktop/) (for Metabase)

```bash
mkdir my-project && cd my-project
pip install getdango
dango init
dango start
```

Open [http://localhost:8800](http://localhost:8800) to see your data platform.

Or use the install script:

```bash
curl -sSL https://getdango.dev/install.sh | bash
```

For detailed installation instructions, see the [documentation](https://docs.getdango.dev).

## Features

- **33 data sources** — Stripe, Google Sheets, Google Analytics, Shopify, PostgreSQL, MySQL, CSV, REST APIs, and more
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

All data stays local in DuckDB. No external warehouse needed.

## Tech Stack

| Component | Tool | Role |
|-----------|------|------|
| Ingestion | [dlt](https://dlthub.com/) | Load data from 33+ sources |
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
