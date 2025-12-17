# Changelog

All notable changes to Dango will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2025-12-17

### Added
- **MVP Release** - First stable release for early adopters
- **Google Ads** - Full OAuth support (tested and working)

### Changed
- Install scripts now available at `getdango.dev/install.sh` (shorter URL)
- Windows support fully tested and documented

### Notes
This is the v0.1.0 MVP release marking Dango as ready for early adopters. All OAuth sources (Google Sheets, GA4, Facebook Ads, Google Ads) are production-ready.

## [0.0.5] - 2025-12-08

### Added
- **Unreferenced Custom Sources Warning**
  - Detects Python files in `custom_sources/` not referenced in `sources.yml`
  - Shows actionable warning in `dango sync`, `dango validate`, and `dango source list`
  - Includes example configuration snippet to help users fix the issue
- **Dry Run Mode for Sync**
  - `dango sync --dry-run` shows what would be synced without executing
- **`__init__.py` in custom_sources/**
  - `dango init` now creates `__init__.py` for proper Python imports

### Fixed
- **Database validation check** now correctly counts tables across all schemas (not just `main`)
- **Model validation count** now shows accurate count instead of "unknown number of"
- **Skip message in sync** now correctly categorizes reasons:
  - "user-customized" for models where marker was removed
  - "tables pending" for tables not yet synced

## [0.0.4] - 2025-12-06

### Fixed
- Fixed version string mismatch between `pyproject.toml` and `__init__.py`

### Changed
- Install scripts now use PyPI (`pip install getdango`) instead of git tags

## [0.0.3] - 2025-12-05

### Added
- **OAuth Authentication**
  - Google Sheets OAuth with browser-based flow
  - Google Analytics (GA4) OAuth with browser-based flow
  - Facebook Ads OAuth with long-lived token support (60-day expiry)
  - `dango auth <provider>` commands for all OAuth sources
  - Inline OAuth prompts during `dango source add` wizard

- **OAuth Token Management**
  - Token expiry tracking and validation
  - Pre-sync expiry warnings (7 days before expiration)
  - Expired token blocking with clear re-auth instructions
  - Facebook token auto-extend for still-valid tokens

- **Pre-flight Validation**
  - OAuth credential validation in `dango validate`
  - Shows pass/warn/fail status for each OAuth source

- **dlt_native Source Type**
  - Support for ANY dlt source via `type: dlt_native`
  - Custom source support from `custom_sources/` directory
  - Full dlt configuration control via `sources.yml`

### Changed
- Shopify support deferred (awaiting upstream dlt updates)
- Google Ads deferred to future release

### Notes
This release adds OAuth support for Google and Facebook data sources, enabling users to connect to Google Sheets, Google Analytics (GA4), and Facebook Ads with browser-based authentication flows.

## [0.0.2] - 2025-11-21

### Added
- **Bootstrap Installer Improvements**
  - Interactive installation mode selection (global vs virtual environment)
  - Custom virtual environment location support
  - Global installation with automatic PATH configuration on all platforms
  - Conflict detection for existing global installations
  - Comprehensive error handling and validation messages
  - PowerShell execution policy auto-detection and fix for Windows
  - PATH refresh in current PowerShell session for immediate use
  - Better shell detection using `$SHELL` variable on Unix systems

- **Windows Platform Support**
  - Complete Windows compatibility throughout the codebase
  - Platform-specific service health checks (HTTP on Windows, Docker on Mac/Linux)
  - Cross-platform file locking (msvcrt on Windows, fcntl on Unix)
  - DuckDB connection retry logic to handle Windows file locking
  - UTF-8 encoding for all file operations to prevent encoding errors

- **Documentation**
  - Complete Windows installation instructions with prerequisites
  - Expanded Python version requirements (3.10-3.12) with installation guides
  - Comprehensive troubleshooting section for both platforms
  - Platform-specific uninstall instructions
  - Enhanced PATH configuration guidance

### Fixed
- **Windows Compatibility**
  - UTF-8 encoding errors in file read/write operations
  - DuckDB file locking by Windows Explorer (dllhost.exe)
  - Docker Desktop performance issues with timeout handling
  - Service health check timeouts (switched to HTTP-based checks on Windows)
  - Frontend timeout handling (5s → 15s for slower Windows operations)
  - Cross-platform hostname detection (replaced Unix-only `os.uname()`)

- **Installer**
  - PATH not updating in current PowerShell session
  - Better Python version detection across all platforms (3.10-3.12 only)
  - User bin path detection on macOS/Linux for global installs
  - Removed direnv dependency to simplify installation UX
  - Fixed success message to acknowledge when venv is already activated

- **Service Management**
  - dbt-docs health check port correction (8080 → 8081)
  - Docker service status detection performance on Windows
  - Async parallel service status checks to improve performance

### Changed
- **Python Support**: Restricted to Python 3.10-3.12 (3.13+ not yet supported due to dependency compatibility, specifically DuckDB binary wheels)
- **Installer UX**: Softer messaging, clearer prompts, better validation and error messages
- **Documentation**: Restructured README with clear platform-specific sections

### Technical Details
- Modified files: 10 core files
- Total changes: +1,490 additions, -357 deletions
- Platform-specific code paths for Windows vs Mac/Linux
- HTTP-based health checks 10x faster than Docker commands on Windows

### Notes
This release focuses on Windows compatibility and installer improvements. All platforms now fully supported with optimized performance characteristics for each OS.

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

### Notes
This is a **preview release** for early feedback. Not recommended for production use.

[Unreleased]: https://github.com/getdango/dango/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/getdango/dango/compare/v0.0.5...v0.1.0
[0.0.5]: https://github.com/getdango/dango/compare/v0.0.4...v0.0.5
[0.0.4]: https://github.com/getdango/dango/compare/v0.0.3...v0.0.4
[0.0.3]: https://github.com/getdango/dango/compare/v0.0.2...v0.0.3
[0.0.2]: https://github.com/getdango/dango/compare/v0.0.1...v0.0.2
[0.0.1]: https://github.com/getdango/dango/releases/tag/v0.0.1
