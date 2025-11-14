# 🍡 Dango

**Production-ready analytics platform in minutes, not weeks**

Dango deploys a complete data stack (DuckDB + dbt + Metabase) to your laptop with one command.

## Installation

**Requirements:** Python 3.10+, Docker Desktop

```bash
pip install getdango
```

## Quick Start

```bash
# Create a new project
mkdir my-analytics
cd my-analytics
dango init

# Add a data source
dango source add

# Start the platform (DuckDB + dbt + Metabase)
dango start

# Load data
dango sync

# Open dashboard
open http://localhost:8800
```

**What you get:**
- **Web UI** at `http://localhost:8800` - Monitor your data pipeline
- **Metabase** for dashboards and SQL queries
- **dbt Docs** for data catalog
- **DuckDB** as your analytics database

## Features (v0.0.1)

**✅ What Works Now:**
- ✅ Full CLI with 9 commands
- ✅ CSV data sources (upload and auto-sync)
- ✅ Stripe integration (tested and working)
- ✅ dbt auto-generation for staging models
- ✅ Web UI with live monitoring
- ✅ Metabase dashboards (auto-configured)
- ✅ File watcher with auto-triggers
- ✅ DuckDB as embedded analytics database

**📝 v0.0.1 is an early preview release**
- Tested with CSV and Stripe sources
- 29 data sources available (most untested)
- OAuth sources planned for v0.1.0
- Not recommended for production use yet

**🚧 Coming in v0.1.0 (Target: Late Nov 2025):**
- OAuth helpers for Google Ads, Facebook Ads, GA4
- REST API framework for custom sources
- Demo project with sample data
- Bootstrap installer script
- Full documentation website

## Architecture

**Data Layers:**
- `raw` - Immutable source of truth (with metadata)
- `staging` - Clean, deduplicated data
- `intermediate` - Reusable business logic
- `marts` - Final business metrics

**Tech Stack:**
- **DuckDB** - Analytics database (embedded, fast)
- **dbt** - SQL transformations
- **dlt** - API integrations (29 sources: 27 verified + CSV + REST)
- **Metabase** - BI dashboards
- **Docker** - Service orchestration
- **FastAPI** - Web UI backend
- **nginx** - Reverse proxy with domain routing

## Target Users

- Solo data professionals
- Fractional consultants
- SMEs needing analytics fast
- Anyone who wants a "real" data stack without the complexity

## Why Dango?

**Most tools force you to choose:**
- ❌ Local-first (limited features) OR Cloud (expensive, complex)
- ❌ No-code (inflexible) OR Full-code (steep learning curve)
- ❌ Fast setup (toy project) OR Production-grade (weeks of work)

**Dango gives you both:**
- ✅ Local-first AND production-ready
- ✅ Wizard-driven AND fully customizable
- ✅ Fast setup AND best practices built-in

## Contributing

We're in active MVP development! Contributions welcome after v0.1.0 releases.

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

## Links

- **PyPI:** https://pypi.org/project/getdango/
- **GitHub:** https://github.com/getdango/dango
- **Issues:** https://github.com/getdango/dango/issues
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)

---

Built with ❤️ for solo data professionals and small teams
