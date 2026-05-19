# Contributing to Dango

Thanks for your interest in contributing to Dango! This guide will help you get started.

## Development Setup

### Prerequisites

- **Python 3.10-3.12** (3.11 recommended)
- **Docker Desktop** (for Metabase)
- **Git**

### Getting Started

```bash
# Clone the repo
git clone https://github.com/getdango/dango.git
cd dango

# Create a virtual environment
python3.11 -m venv venv
source venv/bin/activate  # macOS/Linux
# .\venv\Scripts\Activate.ps1  # Windows

# Install in development mode
pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# Note: always activate the venv before committing —
# pre-commit hooks depend on it

# Run locally
dango start

# Run tests
pytest
```

## Coding Standards

All tool configuration lives in `pyproject.toml`. Key tools:

- **ruff** — linting and formatting
- **mypy** — type checking
- **pre-commit** — runs checks automatically on commit

```bash
# Check before committing
ruff check dango/
ruff format --check dango/
mypy dango/
```

See [STANDARDS.md](STANDARDS.md) for the full coding standards.

## Pull Request Workflow

1. **Fork** the repository
2. **Create a branch** off `v1`:
   ```bash
   git checkout v1
   git pull
   git checkout -b feat/your-feature
   ```
3. **Make your changes** with tests
4. **Run the integration test:**
   ```bash
   scripts/integration_test.sh
   ```
5. **Push and open a PR** against `v1`:
   ```bash
   git push -u origin feat/your-feature
   ```

### PR Guidelines

- Keep PRs focused — one feature or fix per PR
- Include tests for new functionality
- Update documentation if behavior changes
- All CI checks must pass before merge

## Project Structure

```
dango/
├── cli/              # Click CLI commands
├── config/           # Configuration models and loaders
├── ingestion/        # Data ingestion (dlt)
├── transformation/   # dbt integration
├── visualization/    # Metabase integration
├── web/              # FastAPI web server
├── auth/             # Authentication and access control
├── platform/         # Docker, scheduling, cloud deployment
├── governance/       # Schema drift and PII detection
├── notebooks/        # Marimo notebook management
├── analysis/         # Metric monitoring
├── utils/            # Shared utilities
└── security/         # Token encryption
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full system design.

## Code of Conduct

Be respectful and constructive. We're building something useful together — disagreements about code are fine, personal attacks are not. Harassment, discrimination, and disruptive behavior will not be tolerated.

## Getting Help

- **Documentation:** [docs.getdango.dev](https://docs.getdango.dev)
- **Issues:** [GitHub Issues](https://github.com/getdango/dango/issues)
- **Questions:** Open a [Question issue](https://github.com/getdango/dango/issues/new?template=question.yml)
