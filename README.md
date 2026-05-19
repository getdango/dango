# Dango

[![PyPI version](https://img.shields.io/pypi/v/getdango)](https://pypi.org/project/getdango/)
[![Python versions](https://img.shields.io/pypi/pyversions/getdango)](https://pypi.org/project/getdango/)
[![License](https://img.shields.io/github/license/getdango/dango)](LICENSE)
[![GitHub stars](https://img.shields.io/github/stars/getdango/dango)](https://github.com/getdango/dango)

**Open-source data platform for small teams.**

Dango gives you a complete data stack — ingestion, warehouse, transformations, and dashboards — in a single CLI. It combines [dlt](https://dlthub.com/) for data loading, [DuckDB](https://duckdb.org/) as the analytics database, [dbt](https://www.getdbt.com/) for SQL transformations, and [Metabase](https://www.metabase.com/) for dashboards. One `pip install`, one command to start.

<!-- TODO: Add screenshot -->

## Quick Start

**Prerequisites:** Python 3.10-3.12, [Docker](https://docs.docker.com/desktop/) (for Metabase)

```bash
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
- **Web dashboard** — monitor syncs, browse your data catalog, manage sources
- **Metabase integration** — dashboards and SQL queries, auto-configured and ready to use
- **Cloud deployment** — deploy to DigitalOcean or any server with `dango deploy`
- **Authentication** — admin login, user management, 2FA, API keys
- **Schema drift detection** — get alerted when source schemas change
- **PII scanning** — detect personally identifiable information across your tables
- **Notebooks** — Marimo notebooks connected to your DuckDB warehouse
- **Monitoring** — metric tracking with trend detection and drill-downs
- **Scheduled syncs** — cron-based scheduling with retry and timeout handling
- **Webhooks** — Slack notifications for sync results and alerts
- **File watcher** — auto-sync when CSV files change on disk

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

## Links

- [PyPI](https://pypi.org/project/getdango/)
- [Changelog](CHANGELOG.md)
- [Issues](https://github.com/getdango/dango/issues)
