"""dango/cli/init.py

Handles creation of new Dango projects.
"""

from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from dango.config import ConfigLoader, DangoConfig, ProjectContext, SourcesConfig

from .utils import print_error, print_success
from .wizard import ProjectWizard

console = Console()


class ProjectInitializer:
    """Handles Dango project initialization"""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir.resolve()
        self.loader = ConfigLoader(self.project_dir)

    def initialize(self, skip_wizard: bool = False, force: bool = False):
        """
        Initialize a new Dango project.

        Args:
            skip_wizard: Skip interactive wizard, create blank project
            force: Force initialization even if project already exists

        Raises:
            SystemExit: If project already exists and not force
        """
        # Track initialization status
        failures = []
        warnings = []

        # Check if project already exists
        if self.loader.is_dango_project() and not force:
            print_error(
                f"Dango project already exists at {self.project_dir}\nUse --force to reinitialize."
            )
            raise SystemExit(1)

        # Create project directory if it doesn't exist
        if not self.project_dir.exists():
            console.print(f"Creating directory: {self.project_dir}")
            self.project_dir.mkdir(parents=True)

        # Run wizard or create blank config
        if skip_wizard:
            config = self._create_blank_config()
        else:
            wizard = ProjectWizard(self.project_dir)
            config = wizard.run()

        # Wrap initialization in try/catch for atomic rollback on critical failures
        try:
            # Create project structure
            self._create_directory_structure()

            # Save configuration
            self.loader.save_config(config)

            # Create monitors config
            self._create_monitors_config(force=force)

            # Create default .gitignore
            self._create_gitignore()

            # Create pre-commit config
            self._create_pre_commit_config()

            # Create README
            self._create_readme(config)

            # Create COMPATIBILITY.md and SCALABILITY.md
            self._create_compatibility_md()
            self._create_scalability_md()

            # Create docker-compose.yml
            self._create_docker_compose(config)

            # Setup Metabase (Dockerfile + DuckDB driver)
            # NON-CRITICAL: Can retry on 'dango start'
            metabase_success = self._setup_metabase()
            if not metabase_success:
                warnings.append(
                    "DuckDB driver download failed (will retry automatically on 'dango start')"
                )

            # Create dbt project files
            self._create_dbt_project(config)

            # Generate dbt docs (even for empty project)
            # CRITICAL: Required for platform to work correctly
            docs_success = self._generate_dbt_docs()
            if not docs_success:
                # dbt docs is critical - rollback initialization
                print_error("✗ dbt docs generation is required for Dango to work correctly")
                print_error("  Rolling back initialization...")
                self._rollback_initialization()
                print_error("\n❌ Initialization failed")
                console.print("\n[yellow]To fix:[/yellow]")
                console.print("  1. Install dbt-duckdb: pip install dbt-duckdb")
                console.print("  2. Verify dbt works: dbt --version")
                console.print("  3. Retry: dango init")
                raise SystemExit(1)

            # Setup authentication (admin user + auth.yml)
            # NON-CRITICAL: Can set up later via 'dango auth enable'
            auth_success = self._setup_auth(skip_wizard=skip_wizard, force=force)
            if not auth_success:
                warnings.append("Auth setup skipped (run 'dango auth enable' to set up later)")

        except KeyboardInterrupt:
            # User cancelled - rollback
            print_error("\n\n✗ Initialization cancelled by user")
            print_error("  Rolling back...")
            self._rollback_initialization()
            raise SystemExit(1) from None

        except Exception as e:
            # Unexpected error - rollback
            print_error(f"\n\n✗ Unexpected error during initialization: {e}")
            print_error("  Rolling back...")
            self._rollback_initialization()
            raise

        # Install pre-push hook if .git exists (non-critical)
        try:
            self._create_pre_push_hook()
        except Exception:
            pass  # Never fail init over a convenience hook

        # Print success message
        self._print_success_message(warnings=warnings, failures=failures, auth_success=auth_success)

        # Exit with error if critical failures
        if failures:
            raise SystemExit(1)

    def _create_blank_config(self) -> DangoConfig:
        """Create blank configuration"""
        project_name = self.project_dir.name.replace("-", " ").replace("_", " ").title()

        project = ProjectContext(
            name=project_name,
            dango_version=self._get_dango_version(),
            created_by="Unknown",
            purpose="Data analytics project",
        )

        sources = SourcesConfig()

        return DangoConfig(project=project, sources=sources)

    def _create_monitors_config(self, *, force: bool = False) -> None:
        """Create ``.dango/monitors.yml`` with default monitors.

        Fresh init creates an empty config.  ``--force`` re-init with existing
        sources generates templates for configured source types by reading
        the on-disk ``sources.yml`` (not the wizard config, which is blank).
        """
        try:
            from dango.analysis.config import save_monitors_config
            from dango.analysis.models import MonitorsConfig
            from dango.analysis.templates import generate_metrics_for_source

            all_monitors = []  # type: ignore[var-annotated]
            if force:
                existing_config = self.loader.load_config()
                if existing_config.sources and existing_config.sources.sources:
                    for src in existing_config.sources.sources:
                        all_monitors.extend(generate_metrics_for_source(src.type, src.name))

            monitors_config = MonitorsConfig(enabled=True, monitors=all_monitors)
            save_monitors_config(self.project_dir, monitors_config)
        except Exception:
            pass  # Non-critical — never block init

    def _create_directory_structure(self):
        """Create Dango project directory structure"""
        directories = [
            ".dango",
            "data",
            "data/uploads",  # Default CSV upload location
            "data/warehouse",
            "custom_sources",  # Custom dlt sources (dlt_native)
            "dbt",
            "dbt/models",
            "dbt/models/staging",
            "dbt/models/intermediate",
            "dbt/models/marts",
            "dbt/analyses",
            "dbt/tests",
            "dbt/macros",
            "dbt/seeds",
        ]

        for dir_path in directories:
            full_path = self.project_dir / dir_path
            full_path.mkdir(parents=True, exist_ok=True)

        # Create DuckDB database with schemas
        import duckdb

        duckdb_path = self.project_dir / "data" / "warehouse.duckdb"

        # Always ensure schemas exist (CREATE IF NOT EXISTS is idempotent)
        conn = duckdb.connect(str(duckdb_path))
        conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
        conn.execute("CREATE SCHEMA IF NOT EXISTS staging")
        conn.execute("CREATE SCHEMA IF NOT EXISTS intermediate")
        conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
        conn.close()
        console.print(
            "[green]✓[/green] Created DuckDB database with schemas (raw, staging, intermediate, marts)"
        )

        # Create marts README with guidance
        self._create_marts_readme()

        # Create custom_sources __init__.py and README with guidance
        self._create_custom_sources_init()
        self._create_custom_sources_readme()

        # Initialize .dlt/ directory for dlt-native credential storage
        from dango.config.credentials import init_dlt_directory

        init_dlt_directory(self.project_dir)

        print_success("Created project structure")

    def _create_gitignore(self):
        """Create .gitignore file"""
        gitignore_content = """# Dango
.dango/state/
.dango/logs/
.dango/auth.db
.dango/metabase.yml
.dango/dev/
.dango/snapshots/
.dango/*.pid
.dango/*.log
.dango/auth.yml
.dango/dbt_model_status.json
.dango/history/
data/
metabase-data/
metabase-plugins/
metabase.db/
dashboards/
*.db
*.db-shm
*.db-wal
*.duckdb
*.duckdb.wal

# dlt
.dlt/secrets.toml
.dlt/*.db
dbt/.user.yml

# dbt
dbt/target/
dbt/dbt_packages/
dbt/logs/
dbt/profiles.yml

# Auto-generated
.dango/monitors.yml
requirements.txt
COMPATIBILITY.md
SCALABILITY.md
.github/

# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
venv/
ENV/
.venv

# IDEs
.vscode/
.idea/
*.swp
*.swo

# OS
.DS_Store
Thumbs.db

# Secrets
.env
.env.local
secrets/
*.key
"""

        gitignore_path = self.project_dir / ".gitignore"

        # If .gitignore exists, merge
        if gitignore_path.exists():
            with open(gitignore_path, encoding="utf-8") as f:
                existing = f.read()

            if "# Dango" not in existing:
                with open(gitignore_path, "a", encoding="utf-8") as f:
                    f.write("\n" + gitignore_content)
                print_success("Updated .gitignore")
        else:
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(gitignore_content)
            print_success("Created .gitignore")

    def _create_pre_push_hook(self):
        """Create git pre-push hook with checklist reminder.

        Only creates the hook if .git/ exists and the hook file doesn't
        already exist (never overwrites user hooks).
        """
        git_dir = self.project_dir / ".git"
        if not git_dir.is_dir():
            return

        hooks_dir = git_dir / "hooks"
        hooks_dir.mkdir(exist_ok=True)

        hook_path = hooks_dir / "pre-push"
        hook_content = """\
#!/bin/bash
echo ""
echo "  ⚠ Dango pre-push checklist:"
echo "    • Run 'dango validate' to check config and models"
echo "    • Run 'dango dev' to verify model changes against data"
echo "    • Ensure no credentials are committed (.dlt/secrets.toml, .env)"
echo ""
"""
        import os

        try:
            fd = os.open(str(hook_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        except FileExistsError:
            return
        try:
            os.write(fd, hook_content.encode())
        finally:
            os.close(fd)

        console.print("[green]✓[/green] Created .git/hooks/pre-push checklist")

    def _create_pre_commit_config(self):
        """Create .pre-commit-config.yaml with ruff and dango validation hooks."""
        pre_commit_content = """\
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.11.0
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: local
    hooks:
      - id: dango-validate
        name: dango config validate
        entry: dango config validate
        language: system
        pass_filenames: false
        files: '(\\.dango/|dbt/)'
      - id: no-secrets
        name: check for secrets
        entry: >-
          bash -c 'git diff --cached --name-only |
          grep -qE "\\.dlt/secrets\\.toml|\\.env$|\\.env\\.local|cloud_key|\\.key$"
          && echo "ERROR: Sensitive files staged" && exit 1 || exit 0'
        language: system
        pass_filenames: false
"""
        config_path = self.project_dir / ".pre-commit-config.yaml"
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(pre_commit_content)

        print_success("Created .pre-commit-config.yaml")

    def _create_ci_workflow(self):
        """Create GitHub Actions CI workflow for PR validation."""
        workflow_content = """\
name: Dango Validate
on:
  pull_request:
    branches: [main]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install --pre getdango
      - run: dango config validate
      - run: cd dbt && dbt parse --profiles-dir .
      - name: Check for secrets
        run: |
          # Fail if sensitive files are tracked
          if git ls-files | grep -qE '\\.dlt/secrets\\.toml|\\.env$|\\.env\\.local|cloud_key|\\.key$'; then
            echo "ERROR: Sensitive files detected in repository"
            git ls-files | grep -E '\\.dlt/secrets\\.toml|\\.env$|\\.env\\.local|cloud_key|\\.key$'
            exit 1
          fi
"""
        workflow_dir = self.project_dir / ".github" / "workflows"
        workflow_dir.mkdir(parents=True, exist_ok=True)

        workflow_path = workflow_dir / "dango-validate.yml"
        with open(workflow_path, "w", encoding="utf-8") as f:
            f.write(workflow_content)

        print_success("Created .github/workflows/dango-validate.yml")

    def _create_readme(self, config: DangoConfig):
        """Create README.md"""
        readme_content = f"""# {config.project.name}

**Dango Data Project**

## Purpose

{config.project.purpose}

## Stakeholders

"""

        if config.project.stakeholders:
            for stakeholder in config.project.stakeholders:
                readme_content += (
                    f"- **{stakeholder.name}** - {stakeholder.role} ({stakeholder.contact})\n"
                )
        else:
            readme_content += "*(No stakeholders defined)*\n"

        readme_content += f"""
## Data Freshness SLA

{config.project.sla or "*(Not defined)*"}

## Getting Started

{
            config.project.getting_started
            or '''
1. Add data sources: `dango source add`
2. Sync data: `dango sync`
3. Start platform: `dango start`
4. Open dashboards: http://localhost:8800
'''
        }

## Project Structure

```
.
├── .dango/              # Dango configuration
│   ├── project.yml      # Project metadata
│   └── sources.yml      # Data source definitions
├── data/
│   ├── uploads/         # CSV upload directory
│   └── warehouse/       # DuckDB database
├── dbt/                 # dbt transformations
│   └── models/
│       ├── staging/     # Clean, deduplicated data
│       ├── intermediate/# Reusable business logic
│       └── marts/       # Final business metrics
└── README.md           # This file
```

## 📊 Using Your Data in Metabase

### Which Tables Should I Use?

When creating dashboards and reports in Metabase, use tables in this priority order:

1. **staging.*** ✅ **Start here!**
   - Clean, ready-to-use data for dashboards
   - Best for most analysis and visualizations
   - Automatically generated from your data sources

2. **marts.*** ✅ **Pre-built metrics**
   - Business-ready aggregates and facts
   - Optimized for dashboard performance
   - Custom models you create for specific questions

3. **raw.*** ⚠️ **Avoid (engineers only)**
   - Untouched source data
   - Use only for debugging or advanced analysis

### Understanding the Data Layers

| Layer   | Purpose                      | Who Uses It        |
|---------|------------------------------|--------------------|
| raw     | Untouched source data        | Engineers only     |
| staging | Clean, analysis-ready data   | Everyone (start here!) |
| marts   | Business metrics & aggregates| Everyone           |

💡 **Tip:** In Metabase, look for the helpful icons (✅ ⚠️ 📈) in table descriptions to guide you!

## Documentation

- **Project details**: `.dango/project.yml`
- **Data sources**: `.dango/sources.yml`
- **dbt documentation**: Run `dango start` and visit http://localhost:8800/docs

## Limitations

{config.project.limitations or "*(None documented)*"}

---

*Generated with Dango {self._get_dango_version()}*
"""

        readme_path = self.project_dir / "README.md"

        # Only create if doesn't exist
        if not readme_path.exists():
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_content)
            print_success("Created README.md")

    @staticmethod
    def _get_duckdb_version() -> str:
        """Return the installed DuckDB version for COMPATIBILITY.md."""
        try:
            import duckdb

            return duckdb.__version__
        except ImportError:
            return "1.5.x"

    def _create_compatibility_md(self):
        """Create COMPATIBILITY.md with version requirements and platform support."""
        content = f"""\
# Compatibility

Version requirements and platform support for this Dango project.

## Python

- **Required:** Python 3.10, 3.11, or 3.12 (`>=3.10,<3.13`)
- System Python on macOS is 3.9 — use `python3.11` or `python3.12` explicitly

## Operating Systems

| OS | Version | Notes |
|----|---------|-------|
| macOS | 12 (Monterey)+ | Primary development platform |
| Ubuntu | 22.04 LTS | Recommended for cloud deployment |
| Windows | WSL2 only | Native Windows is not supported |

## Docker

- **Docker Engine:** 20.10+
- **Docker Compose:** v2 (ships with Docker Desktop)
- Required for Metabase and the full platform stack (`dango start`)

## Browsers (Metabase + Dango Web UI)

Latest 2 versions of:
- Chrome
- Firefox
- Safari
- Edge

## Core Dependencies

| Component | Version | Notes |
|-----------|---------|-------|
| DuckDB | {self._get_duckdb_version()} | Embedded analytical database |
| dbt-core | 1.10.20 | Data transformation framework |
| dlt | 1.24.0 | Data ingestion toolkit |
| Metabase | v0.59.1 | Business intelligence / dashboards |

## spaCy (Data Governance)

The `en_core_web_sm` language model is required for PII scanning (data governance).

- **Automatic:** Downloaded on first governance scan
- **Manual:** `python -m spacy download en_core_web_sm`

## Upgrade Notes

- Pin your Python version in CI/CD — Dango does not yet support Python 3.13+
- DuckDB minor version upgrades may require a database re-sync (`dango sync`)
- dbt and dlt versions are pinned to compatible ranges in `pyproject.toml`

---

*Generated with Dango {self._get_dango_version()}*
"""

        compat_path = self.project_dir / "COMPATIBILITY.md"
        if not compat_path.exists():
            with open(compat_path, "w", encoding="utf-8") as f:
                f.write(content)
            print_success("Created COMPATIBILITY.md")

    def _create_scalability_md(self):
        """Create SCALABILITY.md with platform limits and upgrade guidance."""
        content = f"""\
# Scalability

Honest guidance on what Dango handles well and when to consider alternatives.

## DuckDB (Data Warehouse)

- **Sweet spot:** Datasets up to ~500 GB on a single machine
- **Row counts:** Handles billions of rows — query performance depends on data shape \
and available RAM
- **Architecture:** In-process columnar engine with no network overhead — excellent for \
analytics workloads
- **Single-writer constraint:** Only one process can write at a time. Dango serializes \
all writes through `DbtLock` (file lock at `.dango/state/dbt.lock`). Concurrent reads \
during writes are fine.

## Concurrent Users

- **Dango Web UI:** Comfortable for ~10-20 concurrent users
- **Metabase:** Has its own connection pool and handles concurrent dashboard viewers \
independently
- **DuckDB reads:** Multiple users can query simultaneously — reads don't block each other

## File Watcher

- Monitors `data/uploads/` for new CSV files
- Debounce interval: 10 minutes (configurable in schedule settings)
- Best suited for small-to-medium file counts — not designed for high-frequency file drops

## Cloud Deployment

- **Default droplet:** `s-2vcpu-4gb` (2 vCPUs, 4 GB RAM) on DigitalOcean
- **Resize:** `dango remote resize` to scale up without data loss
- **Migrate:** `dango remote migrate` to move to a different region

## When to Consider Alternatives

| Signal | Consider |
|--------|----------|
| Data exceeds single-machine disk/RAM | [MotherDuck](https://motherduck.com/) (managed DuckDB in the cloud) |
| Need multi-region or multi-tenant | Cloud-managed warehouse (BigQuery, Snowflake, Redshift) |
| >50 concurrent dashboard users | Dedicated Metabase instance with PostgreSQL backend |
| Real-time streaming ingestion | Kafka/Flink pipeline feeding a separate warehouse |

Dango is designed for small teams with analytical workloads that fit on one machine. \
If you outgrow it, your dbt models and SQL are portable — migration is straightforward.

---

*Generated with Dango {self._get_dango_version()}*
"""

        scale_path = self.project_dir / "SCALABILITY.md"
        if not scale_path.exists():
            with open(scale_path, "w", encoding="utf-8") as f:
                f.write(content)
            print_success("Created SCALABILITY.md")

    def _create_marts_readme(self):
        """Create README.md in marts/ directory with guidance"""
        marts_readme_content = """# Marts Layer

The **marts** layer contains your final business-ready models that answer specific business questions.

## What Goes Here?

### 📊 Fact Tables (`fct_*.sql`)
Central business process tables with numeric measures:
- `fct_orders.sql` - Order transactions with amounts, quantities
- `fct_customer_activity.sql` - User behavior events
- `fct_revenue.sql` - Revenue metrics

### 📁 Dimension Tables (`dim_*.sql`)
Descriptive attributes for analysis:
- `dim_customers.sql` - Customer attributes (name, segment, location)
- `dim_products.sql` - Product catalog with categories
- `dim_dates.sql` - Date calendar with fiscal periods

### 📈 Aggregates (`agg_*.sql`)
Pre-calculated summary tables for dashboards:
- `agg_daily_sales.sql` - Daily sales rollups
- `agg_customer_lifetime_value.sql` - Customer metrics
- `agg_product_performance.sql` - Product analytics

## Quick Start

Create your first mart:

```sql
-- dbt/models/marts/fct_orders.sql

{{ config(
    materialized='table',
    schema='marts'
) }}

SELECT
    order_id,
    customer_id,
    order_date,
    total_amount,
    order_status
FROM {{ ref('orders') }}  -- Reference staging model
WHERE order_status != 'cancelled'
```

Then reference it in dashboards or other models:

```sql
SELECT * FROM marts.fct_orders
```

## Best Practices

✅ **DO:**
- Use clear naming: `fct_`, `dim_`, `agg_` prefixes
- Document business logic in comments
- Add dbt tests for data quality
- Materialize as tables (performance)

❌ **DON'T:**
- Put raw data transformations here (use staging)
- Create circular dependencies
- Hard-code values (use seeds or variables)

## Need Help?

- Run `dango model add` to use the modeling wizard
- Check dbt docs: http://localhost:8800/dbt-docs (after `dango start`)
- See examples in staging/ and intermediate/ layers

---
*Auto-generated by Dango*
"""
        marts_readme_path = self.project_dir / "dbt" / "models" / "marts" / "README.md"

        if not marts_readme_path.exists():
            with open(marts_readme_path, "w", encoding="utf-8") as f:
                f.write(marts_readme_content)
            console.print("[green]✓[/green] Created marts/README.md with guidance")

    def _create_custom_sources_init(self):
        """Create __init__.py in custom_sources/ directory for Python imports"""
        init_path = self.project_dir / "custom_sources" / "__init__.py"

        if not init_path.exists():
            with open(init_path, "w", encoding="utf-8") as f:
                f.write("# Custom dlt sources for this project\n")

    def _create_custom_sources_readme(self):
        """Create README.md in custom_sources/ directory with guidance"""
        custom_sources_readme_content = """# Custom Sources (dlt Native)

This directory is for **advanced users** who want to:
- Use dlt sources not in Dango's registry
- Write custom dlt sources
- Have full control over dlt source configuration

## Quick Example

1. **Create a custom source file** (`custom_sources/my_api.py`):

```python
import dlt
from dlt.sources.helpers import requests

@dlt.source
def my_api_source(api_key: str = dlt.secrets.value):
    \"\"\"Load data from my custom API\"\"\"

    @dlt.resource(write_disposition="merge", primary_key="id")
    def users():
        response = requests.get(
            "https://api.example.com/users",
            headers={"Authorization": f"Bearer {api_key}"}
        )
        yield response.json()

    return users

```

2. **Configure in `.dango/sources.yml`**:

```yaml
sources:
  - name: my_api
    type: dlt_native
    enabled: true
    dlt_native:
      source_module: my_api  # Looks in custom_sources/my_api.py
      source_function: my_api_source
      function_kwargs:
        api_key: "env:MY_API_KEY"  # From .env or .dlt/secrets.toml
```

3. **Add credentials to `.dlt/secrets.toml`**:

```toml
[sources.my_api]
api_key = "your_api_key_here"
```

4. **Sync**: `dango sync --source my_api`

## Using dlt Verified Sources (Not in Registry)

Install a dlt source that's not in Dango's registry:

```bash
pip install dlt[zendesk]
```

Configure in `.dango/sources.yml`:

```yaml
sources:
  - name: zendesk_custom
    type: dlt_native
    enabled: true
    dlt_native:
      source_module: zendesk  # Installed dlt package
      source_function: zendesk_support
      function_kwargs:
        subdomain: "mycompany"
        credentials:
          email: "env:ZENDESK_EMAIL"
          token: "env:ZENDESK_TOKEN"
```

## File Structure

```
custom_sources/
├── README.md          # This file
├── my_api.py          # Your custom source
├── another_source.py  # Another custom source
└── helpers.py         # Shared helper functions
```

## Important Notes

⚠️ **Advanced Feature**
- Requires Python/dlt knowledge
- No wizard support (file-based config only)
- Manual troubleshooting required

📚 **Learn More**
- dlt Documentation: https://dlthub.com/docs
- Dango Advanced Guide: docs/ADVANCED_USAGE.md
- Registry Bypass Guide: docs/REGISTRY_BYPASS.md

---
*Auto-generated by Dango*
"""
        custom_sources_readme_path = self.project_dir / "custom_sources" / "README.md"

        if not custom_sources_readme_path.exists():
            with open(custom_sources_readme_path, "w", encoding="utf-8") as f:
                f.write(custom_sources_readme_content)
            console.print("[green]✓[/green] Created custom_sources/README.md with guidance")

    def _create_docker_compose(self, config: DangoConfig):
        """Create docker-compose.yml from template"""
        from jinja2 import Environment, PackageLoader

        env = Environment(loader=PackageLoader("dango", "templates"))
        template = env.get_template("docker-compose.yml.j2")

        content = template.render(
            project_name=config.project.name.lower().replace(" ", "-"),
            metabase_port=config.platform.metabase_port,
            dbt_docs_port=config.platform.dbt_docs_port,
        )

        docker_compose_path = self.project_dir / "docker-compose.yml"
        with open(docker_compose_path, "w", encoding="utf-8") as f:
            f.write(content)

        print_success("Created docker-compose.yml")

    def _setup_metabase(self) -> bool:
        """
        Setup Metabase with DuckDB support

        Returns:
            True if successful, False if driver download failed
        """
        import urllib.request

        from jinja2 import Environment, PackageLoader

        console.print("Setting up Metabase with DuckDB support...")

        # Copy Dockerfile.metabase from templates
        env = Environment(loader=PackageLoader("dango", "templates"))
        dockerfile_template = env.get_template("Dockerfile.metabase")
        dockerfile_content = dockerfile_template.render()

        dockerfile_path = self.project_dir / "Dockerfile.metabase"
        with open(dockerfile_path, "w", encoding="utf-8") as f:
            f.write(dockerfile_content)

        console.print("[green]✓[/green] Created Dockerfile.metabase")

        # Copy entrypoint.sh from templates (required by Dockerfile.metabase)
        entrypoint_template = env.get_template("entrypoint.sh")
        entrypoint_content = entrypoint_template.render()

        entrypoint_path = self.project_dir / "entrypoint.sh"
        with open(entrypoint_path, "w", encoding="utf-8") as f:
            f.write(entrypoint_content)
        entrypoint_path.chmod(0o755)

        console.print("[green]✓[/green] Created entrypoint.sh")

        # Create metabase-plugins directory
        plugins_dir = self.project_dir / "metabase-plugins"
        plugins_dir.mkdir(exist_ok=True)

        # Download DuckDB driver (MotherDuck official driver, version-matched)
        from dango.utils.driver import (
            METABASE_DUCKDB_DRIVER_VERSION,
            driver_needs_update,
            get_duckdb_driver_url,
            write_driver_version,
        )

        duckdb_driver_path = plugins_dir / "duckdb.metabase-driver.jar"
        needs_download = not duckdb_driver_path.exists() or driver_needs_update(plugins_dir)

        if needs_download:
            # Delete stale driver if version mismatch
            if duckdb_driver_path.exists():
                duckdb_driver_path.unlink()

            driver_url = get_duckdb_driver_url()
            driver_downloaded = False

            # Retry same URL 3 times (network issues are transient)
            import time

            from rich.progress import (
                BarColumn,
                DownloadColumn,
                Progress,
                TextColumn,
                TransferSpeedColumn,
            )

            for attempt in range(3):
                try:
                    if attempt > 0:
                        console.print(f"    Retry {attempt}/2...")
                        time.sleep(2)  # Wait before retry
                    with urllib.request.urlopen(driver_url, timeout=120) as response:
                        total = int(response.headers.get("Content-Length", 0))
                        with Progress(
                            TextColumn("[progress.description]{task.description}"),
                            BarColumn(),
                            DownloadColumn(),
                            TransferSpeedColumn(),
                            console=console,
                        ) as progress:
                            task = progress.add_task(
                                "Downloading DuckDB driver",
                                total=total if total > 0 else None,
                            )
                            with open(duckdb_driver_path, "wb") as f:
                                while True:
                                    chunk = response.read(65536)
                                    if not chunk:
                                        break
                                    f.write(chunk)
                                    progress.advance(task, len(chunk))
                    write_driver_version(plugins_dir, METABASE_DUCKDB_DRIVER_VERSION)
                    console.print(
                        f"[green]✓[/green] Downloaded DuckDB driver ({duckdb_driver_path.stat().st_size // 1024 // 1024}MB)"
                    )
                    driver_downloaded = True
                    break
                except Exception:
                    duckdb_driver_path.unlink(missing_ok=True)
                    if attempt == 2:  # Last attempt failed
                        break
                    continue

            if not driver_downloaded:
                print_error("✗ Failed to download DuckDB driver (network issue)")
                console.print(
                    "    [yellow]Don't worry![/yellow] The driver will be downloaded automatically when you run:"
                )
                console.print("    [bold cyan]dango start[/bold cyan]")
                print_success("Metabase setup complete (driver pending)")
                return False
        else:
            console.print("[green]✓[/green] DuckDB driver already exists")

        print_success("Metabase setup complete")
        return True

    def _create_dbt_project(self, config: DangoConfig):
        """Create dbt project configuration files"""
        # Sanitize project name for dbt (lowercase, underscores only)
        dbt_project_name = config.project.name.lower().replace(" ", "_").replace("-", "_")

        # Create dbt_project.yml
        dbt_project_content = f"""# dbt Project Configuration
# Auto-generated by Dango

name: '{dbt_project_name}'
version: '1.0.0'
config-version: 2

# Project profile
profile: '{dbt_project_name}'

# Directories
model-paths: ["models"]
analysis-paths: ["analyses"]
test-paths: ["tests"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]

target-path: "target"
clean-targets:
  - "target"
  - "dbt_packages"
  - "logs"

# Model configurations
models:
  {dbt_project_name}:
    # Staging models (clean, deduplicated source data)
    staging:
      +materialized: table
      +schema: staging

    # Intermediate models (reusable business logic)
    intermediate:
      +materialized: table
      +schema: intermediate

    # Marts models (final business metrics)
    marts:
      +materialized: table
      +schema: marts

# Seeds configuration
seeds:
  {dbt_project_name}:
    +quote_columns: false

# Documentation
docs-paths: ["docs"]

# Logging
on-run-start:
  - "{{{{ log('Starting dbt run', info=true) }}}}"

on-run-end:
  - "{{{{ log('Completed dbt run', info=true) }}}}"
"""

        dbt_project_path = self.project_dir / "dbt" / "dbt_project.yml"
        with open(dbt_project_path, "w", encoding="utf-8") as f:
            f.write(dbt_project_content)

        # Create profiles.yml
        profiles_content = f"""# dbt Profile Configuration for DuckDB
# Connects to local DuckDB warehouse

{dbt_project_name}:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: ../data/warehouse.duckdb
      schema: main
      threads: 4

      # DuckDB-specific settings
      extensions:
        - httpfs
        - parquet

      # Settings
      settings:
        memory_limit: 4GB
        threads: 4
"""

        profiles_path = self.project_dir / "dbt" / "profiles.yml"
        with open(profiles_path, "w", encoding="utf-8") as f:
            f.write(profiles_content)

        # Create dbt macro for clean schema naming (removes main_ prefix)
        macro_content = """{#
    Override dbt's default schema naming to use clean schema names

    Default behavior: custom_schema_name="staging" → "main_staging"
    Our behavior: custom_schema_name="staging" → "staging"

    This gives us clean schemas: raw, staging, marts (not main_staging, main_marts)
#}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
"""
        macro_path = self.project_dir / "dbt" / "macros" / "get_custom_schema.sql"
        with open(macro_path, "w", encoding="utf-8") as f:
            f.write(macro_content)

        print_success("Created dbt project files (dbt_project.yml, profiles.yml, macros)")

    def _generate_dbt_docs(self) -> bool:
        """
        Generate dbt documentation (works even for empty project)

        Returns:
            True if successful, False if generation failed
        """
        import shutil
        import subprocess
        import sys

        console.print("Generating dbt documentation...")

        dbt_dir = self.project_dir / "dbt"

        # Find dbt command - prefer venv's dbt to avoid using system dbt
        # (system dbt may use ~/.dbt/profiles.yml which won't have this project's profile)
        venv_dbt = Path(sys.executable).parent / "dbt"
        if venv_dbt.exists():
            dbt_cmd = str(venv_dbt)
        else:
            dbt_cmd = shutil.which("dbt") or "dbt"

        try:
            # Run dbt docs generate
            result = subprocess.run(
                [
                    dbt_cmd,
                    "docs",
                    "generate",
                    "--project-dir",
                    str(dbt_dir),
                    "--profiles-dir",
                    str(dbt_dir),
                ],
                cwd=dbt_dir,
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] dbt docs generated successfully")

                # Check that index.html was created
                index_path = dbt_dir / "target" / "index.html"
                if index_path.exists():
                    console.print(
                        "[green]✓[/green] Documentation available at dbt/target/index.html"
                    )
                    return True
                else:
                    print_error("✗ index.html not found after generation")
                    return False
            else:
                # dbt outputs errors to stdout, not stderr
                error_output = result.stderr or result.stdout or "Unknown error"
                print_error(f"✗ dbt docs generate failed: {error_output}")
                console.print("    You can generate docs later with: cd dbt && dbt docs generate")
                return False

        except subprocess.TimeoutExpired:
            print_error("✗ dbt docs generate timed out")
            console.print("    You can generate docs later with: cd dbt && dbt docs generate")
            return False
        except FileNotFoundError:
            print_error("✗ dbt command not found")
            console.print("    Install dbt-duckdb: pip install dbt-duckdb")
            return False
        except Exception as e:
            print_error(f"✗ Failed to generate dbt docs: {e}")
            console.print("    You can generate docs later with: cd dbt && dbt docs generate")
            return False

    def _write_auth_to_project_yml(self) -> None:
        """Write auth.enabled to project.yml for user visibility."""
        project_yml = self.project_dir / ".dango" / "project.yml"
        if project_yml.exists():
            data = self.loader.load_yaml(project_yml)
            data.setdefault("auth", {})["enabled"] = True
            self.loader.save_yaml(data, project_yml)

    def _setup_auth(self, *, skip_wizard: bool = False, force: bool = False) -> bool:
        """Set up authentication: create admin user and enable auth.

        Runs database migrations to create auth.db, then either prompts for
        admin credentials (interactive) or generates a random admin
        (skip-wizard mode).

        Args:
            skip_wizard: If True, generate random admin credentials.
            force: If True, skip admin creation when admins already exist.

        Returns:
            True if auth was set up successfully, False otherwise.
        """
        import os
        import re

        import click

        from dango.auth.admin import (
            ensure_admin,
            format_credentials_panel,
            get_auth_db_path,
            set_auth_enabled,
        )
        from dango.auth.database import create_user, list_users
        from dango.auth.models import Role, User
        from dango.auth.security import (
            check_password_strength,
            generate_temp_password,
            hash_password,
        )
        from dango.migrations import apply_all_pending

        console.print("\nSetting up authentication...")

        try:
            # Create auth.db via migrations framework
            apply_all_pending(self.project_dir)

            db_path = get_auth_db_path(self.project_dir)

            # On --force re-init, skip admin creation if admins already exist
            if force:
                existing_users = list_users(db_path, active_only=True)
                existing_admins = [u for u in existing_users if u.role == Role.ADMIN]
                if existing_admins:
                    # Just ensure auth.yml is written
                    set_auth_enabled(self.project_dir, enabled=True)
                    self._write_auth_to_project_yml()
                    console.print("[green]✓[/green] Auth already configured (admin exists)")
                    return True

            if skip_wizard:
                # Non-interactive: generate random admin
                import os

                email = os.environ.get("DANGO_ADMIN_EMAIL", "admin@localhost")
                result = ensure_admin(db_path, email=email)
                if result is not None:
                    user, password = result
                    set_auth_enabled(self.project_dir, enabled=True)
                    self._write_auth_to_project_yml()
                    console.print()
                    console.print(format_credentials_panel(user.email, password))
                    console.print()
                else:
                    set_auth_enabled(self.project_dir, enabled=True)
                    self._write_auth_to_project_yml()
                    console.print("[green]✓[/green] Auth enabled (admin already exists)")
                return True

            # Interactive: prompt for admin credentials
            email_re = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")

            console.print("  Create the admin account for your project.\n")

            # Email
            while True:
                email = click.prompt("  Admin email")
                if not email_re.match(email):
                    console.print("  [red]Invalid email format.[/red]")
                    continue
                confirm_email = click.prompt("  Confirm email")
                if email.lower() != confirm_email.lower():
                    console.print("  [red]Emails don't match.[/red]")
                    continue
                break

            # Password (from env or prompt)
            env_password = os.environ.get("DANGO_ADMIN_PASSWORD")
            if env_password:
                issues = check_password_strength(env_password, email=email)
                if issues:
                    console.print(f"  [red]DANGO_ADMIN_PASSWORD is weak:[/red] {'; '.join(issues)}")
                    console.print("  [yellow]Skipping auth setup.[/yellow]")
                    return False
                password = env_password
                console.print("  [dim]Using password from DANGO_ADMIN_PASSWORD env var.[/dim]")
            else:
                password = generate_temp_password()

            # Create admin user
            from datetime import datetime, timezone

            must_change_password = env_password is None  # True for auto-gen, False for env var
            user = User(
                email=email,
                password_hash=hash_password(password),
                role=Role.ADMIN,
                must_change_password=must_change_password,
                password_changed_at=datetime.now(timezone.utc),
            )
            create_user(db_path, user)

            # Enable auth
            set_auth_enabled(self.project_dir, enabled=True)
            self._write_auth_to_project_yml()

            console.print()
            console.print(format_credentials_panel(email, password, title="Admin account created"))
            console.print()
            return True

        except Exception as e:
            print_error(f"Auth setup failed: {e}")
            console.print("  [yellow]You can set up auth later with:[/yellow] dango auth enable")
            return False

    def _rollback_initialization(self):
        """
        Rollback project initialization by removing created files/directories.

        Called when critical initialization steps fail to prevent partial state.
        Only removes Dango-created files, preserves any user files that existed before.
        """
        import shutil

        console.print("[dim]Cleaning up partial initialization...[/dim]")

        # List of files/directories to remove (in order)
        cleanup_targets = [
            ".dango",
            "data",
            "dbt",
            "metabase-plugins",
            "docker-compose.yml",
            "Dockerfile.metabase",
            "entrypoint.sh",
            "README.md",  # Only if created by us
            "COMPATIBILITY.md",
            "SCALABILITY.md",
            ".gitignore",  # Only if created by us
        ]

        for target in cleanup_targets:
            target_path = self.project_dir / target
            try:
                if target_path.exists():
                    if target_path.is_dir():
                        shutil.rmtree(target_path)
                        console.print(f"[dim]  ✓ Removed {target}/[/dim]")
                    else:
                        target_path.unlink()
                        console.print(f"[dim]  ✓ Removed {target}[/dim]")
            except Exception as e:
                # Log but don't fail on cleanup errors
                console.print(f"[dim]  ⚠ Could not remove {target}: {e}[/dim]")

        console.print("[green]✓[/green] Cleanup complete")

    def _get_dango_version(self) -> str:
        """Get Dango version"""
        from dango import __version__

        return __version__

    def _print_success_message(self, warnings=None, failures=None, auth_success=True):
        """Print success message with next steps"""
        warnings = warnings or []
        failures = failures or []

        console.print()

        # Determine overall status
        if failures:
            title = "❌ Initialization Failed"
            border_style = "red"
            status_msg = "[bold red]✗ Project initialization failed[/bold red]"
        elif warnings:
            title = "⚠️  Initialization Completed with Warnings"
            border_style = "yellow"
            status_msg = "[bold yellow]⚠ Project initialized with some warnings[/bold yellow]"
        else:
            title = "🎉 Success"
            border_style = "green"
            status_msg = "[bold green]✓ Project initialized successfully![/bold green]"

        # Build message
        message = f"{status_msg}\n\n"

        # Add warnings if any
        if warnings:
            message += "[bold yellow]Warnings:[/bold yellow]\n"
            for warning in warnings:
                message += f"⚠ {warning}\n"
            message += "\n"

        # Add failures if any
        if failures:
            message += "[bold red]Errors:[/bold red]\n"
            for failure in failures:
                message += f"✗ {failure}\n"
            message += "\n"

        # Add next steps (only if not failed)
        if not failures:
            if auth_success:
                message += "[dim]Auth is enabled — log in with your admin credentials on first visit.[/dim]\n\n"
            message += "[bold]Next steps:[/bold]\n\n"

            # Check if user is already in the project directory
            already_in_dir = self.project_dir == Path.cwd()

            if already_in_dir:
                # Pattern B: User already in directory, skip cd step
                message += "1. dango source add     # Add your first data source\n"
                message += "2. dango sync           # Fetch data from sources to database\n"
                message += "3. dango start          # Start platform\n"
                message += "4. Open http://localhost:8800"
            else:
                # Pattern A: User needs to cd into directory first
                message += f"1. cd {self.project_dir.name}                # Navigate to project directory\n"
                message += "2. dango source add     # Add your first data source\n"
                message += "3. dango sync           # Fetch data from sources to database\n"
                message += "4. dango start          # Start platform\n"
                message += "5. Open http://localhost:8800"

        console.print(Panel(message, title=title, border_style=border_style))

        # Print detect-secrets recommendation after main panel
        if not failures:
            console.print(
                "[dim]Tip: Add secret scanning to prevent accidentally committing credentials:\n"
                "  pip install pre-commit detect-secrets\n"
                "  detect-secrets scan > .secrets.baseline\n"
                "  # Add detect-secrets hook to .pre-commit-config.yaml[/dim]"
            )

        console.print()


def init_project(project_dir: Path, skip_wizard: bool = False, force: bool = False):
    """
    Initialize a new Dango project.

    Args:
        project_dir: Directory to initialize project in
        skip_wizard: Skip interactive wizard
        force: Force initialization even if project exists
    """
    initializer = ProjectInitializer(project_dir)
    initializer.initialize(skip_wizard=skip_wizard, force=force)
