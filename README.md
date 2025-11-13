# 🍡 Dango

**Production-ready analytics platform in minutes, not weeks**

Dango deploys a complete data stack (DuckDB + dbt + Metabase) to your laptop with one command.

## Status

🚀 **MVP Development** - 75% Complete | Target Release: **November 29, 2025**

- ✅ **Implemented:** CLI (9 commands), 29 data sources, Web UI, dbt auto-generation
- ⏳ **Remaining:** Auto-triggers, demo project, bootstrap script, PyPI packaging
- 📅 **Timeline:** 4 weeks to v0.1.0 release (see [TIMELINE.md](TIMELINE.md))

## Quick Start (Coming Soon)

```bash
# One-line install
curl -sSL getdango.dev/bootstrap | bash -s my-analytics

# Interactive setup
cd my-analytics
dango init .

# Add data sources
dango source add  # Interactive wizard

# Start platform
dango start

# Load data
dango sync

# Open dashboards
open http://my-analytics.dango
```

## Features

### ✅ Implemented
- **CLI Framework:** 9 commands (`init`, `start`, `stop`, `status`, `info`, `config`, `source`, `sync`, `dashboard`)
- **Data Sources:** 29 sources integrated (27 dlt verified sources + CSV + REST API)
- **Web UI:** FastAPI backend with live pipeline monitoring
- **dbt Auto-Generation:** Automatically generate staging models from source schemas
- **Metabase Dashboards:** API-based dashboard provisioning
- **Incremental Loading:** CSV with metadata tracking and 4 dedup strategies
- **Config Validation:** Pydantic-based schema validation with friendly errors

### 🚧 Coming in v0.1.0 (MVP Release: Nov 29)
- **Network Architecture:** `<project>.dango` domains with shared port 80
- **Auto-Triggers:** File watcher with 10-minute debounce → auto-sync → auto-dbt
- **Demo Project:** Sample data + pre-built dashboards (`dango demo create`)
- **Dashboard Persistence:** Export/import workflow for git
- **Metabase Auto-Setup:** Zero-config with auto-login and organization branding
- **dbt Modeling Wizard:** Template-based fact/dimension table creation
- **OAuth Helpers:** Guided flows for Facebook, Google
- **Bootstrap Install:** One-command setup via curl
- **PyPI Package:** Install via `pip install dango-data`

### 📋 Post-MVP (v0.2+)
- Orchestration with Prefect Cloud
- Cloud deployment options (Railway, Render, DigitalOcean)
- Advanced monitoring and alerting
- Multi-user collaboration features

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

## Development

```bash
# Clone repo
git clone https://github.com/getdango/dango
cd dango

# Install in development mode
pip install -e ".[dev]"

# Run CLI
dango --help

# Run tests (coming soon)
pytest
```

## Documentation

- **[MVP Roadmap](MVP_ROADMAP.md)** - Complete MVP specifications and timeline
- **[Timeline](TIMELINE.md)** - Visual 4-week roadmap to v0.1.0 release
- **[Implementation Progress](IMPLEMENTATION_PROGRESS.md)** - Development log
- **[Architecture](architecture.md)** - System design overview
- **[CSV Loading Design](CSV_LOADING_DESIGN_SUMMARY.md)** - Phase 1 decisions

## Contributing

We're in active MVP development! Contributions welcome after v0.1.0 releases (Nov 29, 2025).

See [CONTRIBUTING.md](CONTRIBUTING.md) (coming soon) for guidelines.

## License

Apache 2.0 - See [LICENSE](LICENSE) for details.

## Links

- **Homepage:** https://getdango.dev (coming soon)
- **Docs:** https://docs.getdango.dev (coming soon)
- **GitHub:** https://github.com/getdango/dango
- **Issues:** https://github.com/getdango/dango/issues

---

Built with ❤️ for the data community
