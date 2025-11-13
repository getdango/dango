# Changelog

All notable changes to Dango will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.0.1] - 2025-11-14

### Added
- Initial pre-MVP preview release
- CLI framework with 9 core commands
- CSV and Stripe data source integration (fully tested)
- dbt auto-generation for staging models
- Web UI with FastAPI backend and live monitoring
- Metabase integration with auto-setup
- File watcher with auto-triggers for CSV and dbt changes
- Interactive wizards for project setup and source configuration
- DuckDB as embedded analytics database
- Docker Compose orchestration for services

### Core Commands
- `dango init` - Initialize new project with interactive wizard
- `dango source add/list/remove` - Manage data sources
- `dango sync` - Load data from sources with auto-dbt generation
- `dango start/stop/status` - Service management
- `dango run` - Run dbt transformations
- `dango model add` - Create intermediate/marts models with wizard
- `dango dashboard export/import` - Dashboard version control
- `dango validate` - Comprehensive project validation
- `dango config` - Configuration management

### Known Limitations
- **Only CSV and Stripe sources tested** in v0.0.1
- Other dlt sources available but not verified
- OAuth sources (Google Ads, Meta Ads, GA4, Shopify) planned for v0.1.0
- REST API framework for custom sources planned for v0.1.0
- Bootstrap installer script coming in v0.1.0
- Demo project coming in v0.1.0

### Notes
This is a **preview release** for early feedback. Not recommended for production use.
Full MVP (v0.1.0) with OAuth and complete documentation targeted for late November 2025.

[Unreleased]: https://github.com/getdango/dango/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/getdango/dango/releases/tag/v0.0.1
