"""
Project Initialization

Handles creation of new Dango projects.
"""

import os
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from dango.config import ConfigLoader, DangoConfig, ProjectContext, SourcesConfig
from .wizard import ProjectWizard
from .utils import print_success, print_error, print_info, confirm

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
                f"Dango project already exists at {self.project_dir}\n"
                f"Use --force to reinitialize."
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

            # Create default .gitignore
            self._create_gitignore()

            # Create README
            self._create_readme(config)

            # Create docker-compose.yml
            self._create_docker_compose(config)

            # Setup Metabase (Dockerfile + DuckDB driver)
            # NON-CRITICAL: Can retry on 'dango start'
            metabase_success = self._setup_metabase()
            if not metabase_success:
                warnings.append("DuckDB driver download failed (will retry automatically on 'dango start')")

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

        except KeyboardInterrupt:
            # User cancelled - rollback
            print_error("\n\n✗ Initialization cancelled by user")
            print_error("  Rolling back...")
            self._rollback_initialization()
            raise SystemExit(1)

        except Exception as e:
            # Unexpected error - rollback
            print_error(f"\n\n✗ Unexpected error during initialization: {e}")
            print_error("  Rolling back...")
            self._rollback_initialization()
            raise

        # Print success message
        self._print_success_message(warnings=warnings, failures=failures)

        # Exit with error if critical failures
        if failures:
            raise SystemExit(1)

    def _create_blank_config(self) -> DangoConfig:
        """Create blank configuration"""
        project_name = self.project_dir.name.replace('-', ' ').replace('_', ' ').title()

        project = ProjectContext(
            name=project_name,
            dango_version=self._get_dango_version(),
            created_by="Unknown",
            purpose="Data analytics project",
        )

        sources = SourcesConfig()

        return DangoConfig(project=project, sources=sources)

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
        console.print("[green]✓[/green] Created DuckDB database with schemas (raw, staging, intermediate, marts)")

        # Create marts README with guidance
        self._create_marts_readme()

        # Create custom_sources __init__.py and README with guidance
        self._create_custom_sources_init()
        self._create_custom_sources_readme()

        # Initialize .dlt/ directory for dlt-native credential storage
        from dango.config.credentials import init_dlt_directory
        init_dlt_directory(self.project_dir)

        print_success(f"Created project structure")

    def _create_gitignore(self):
        """Create .gitignore file"""
        gitignore_content = """# Dango
.dango/state/
.dango/metabase.yml
data/warehouse/
data/uploads/
dashboards/  # Old export location (deprecated, use 'dango metabase save' instead)
*.db
*.db-shm
*.db-wal

# dbt
dbt/target/
dbt/dbt_packages/
dbt/logs/

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
"""

        gitignore_path = self.project_dir / ".gitignore"

        # If .gitignore exists, merge
        if gitignore_path.exists():
            with open(gitignore_path, 'r', encoding='utf-8') as f:
                existing = f.read()

            if "# Dango" not in existing:
                with open(gitignore_path, 'a', encoding='utf-8') as f:
                    f.write("\n" + gitignore_content)
                print_success("Updated .gitignore")
        else:
            with open(gitignore_path, 'w', encoding='utf-8') as f:
                f.write(gitignore_content)
            print_success("Created .gitignore")

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
                readme_content += f"- **{stakeholder.name}** - {stakeholder.role} ({stakeholder.contact})\n"
        else:
            readme_content += "*(No stakeholders defined)*\n"

        readme_content += f"""
## Data Freshness SLA

{config.project.sla or '*(Not defined)*'}

## Getting Started

{config.project.getting_started or '''
1. Add data sources: `dango source add`
2. Sync data: `dango sync`
3. Start platform: `dango start`
4. Open dashboards: http://dango.local or http://localhost
'''}

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
- **dbt documentation**: Run `dango start` and visit http://dango.local/docs

## Limitations

{config.project.limitations or '*(None documented)*'}

---

*Generated with Dango {self._get_dango_version()}*
"""

        readme_path = self.project_dir / "README.md"

        # Only create if doesn't exist
        if not readme_path.exists():
            with open(readme_path, 'w', encoding='utf-8') as f:
                f.write(readme_content)
            print_success("Created README.md")

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
            with open(marts_readme_path, 'w', encoding='utf-8') as f:
                f.write(marts_readme_content)
            console.print("[green]✓[/green] Created marts/README.md with guidance")

    def _create_custom_sources_init(self):
        """Create __init__.py in custom_sources/ directory for Python imports"""
        init_path = self.project_dir / "custom_sources" / "__init__.py"

        if not init_path.exists():
            with open(init_path, 'w', encoding='utf-8') as f:
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
            with open(custom_sources_readme_path, 'w', encoding='utf-8') as f:
                f.write(custom_sources_readme_content)
            console.print("[green]✓[/green] Created custom_sources/README.md with guidance")

    def _create_docker_compose(self, config: DangoConfig):
        """Create docker-compose.yml from template"""
        from jinja2 import Environment, PackageLoader

        env = Environment(loader=PackageLoader('dango', 'templates'))
        template = env.get_template('docker-compose.yml.j2')

        content = template.render(
            project_name=config.project.name.lower().replace(' ', '-'),
            metabase_port=config.platform.metabase_port,
            dbt_docs_port=config.platform.dbt_docs_port
        )

        docker_compose_path = self.project_dir / "docker-compose.yml"
        with open(docker_compose_path, 'w', encoding='utf-8') as f:
            f.write(content)

        print_success("Created docker-compose.yml")

    def _setup_metabase(self) -> bool:
        """
        Setup Metabase with DuckDB support

        Returns:
            True if successful, False if driver download failed
        """
        import shutil
        import urllib.request
        from jinja2 import Environment, PackageLoader

        console.print("Setting up Metabase with DuckDB support...")

        # Copy Dockerfile.metabase from templates
        env = Environment(loader=PackageLoader('dango', 'templates'))
        dockerfile_template = env.get_template('Dockerfile.metabase')
        dockerfile_content = dockerfile_template.render()

        dockerfile_path = self.project_dir / "Dockerfile.metabase"
        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(dockerfile_content)

        console.print("[green]✓[/green] Created Dockerfile.metabase")

        # Create metabase-plugins directory
        plugins_dir = self.project_dir / "metabase-plugins"
        plugins_dir.mkdir(exist_ok=True)

        # Download DuckDB driver (MotherDuck official driver)
        driver_url = "https://github.com/motherduckdb/metabase_duckdb_driver/releases/download/1.4.1.0/duckdb.metabase-driver.jar"
        duckdb_driver_path = plugins_dir / "duckdb.metabase-driver.jar"

        if not duckdb_driver_path.exists():
            console.print("⏳ Downloading DuckDB driver (70MB, this may take a moment)...")
            driver_downloaded = False

            # Retry same URL 3 times (network issues are transient)
            import time
            for attempt in range(3):
                try:
                    if attempt > 0:
                        console.print(f"    Retry {attempt}/2...")
                        time.sleep(2)  # Wait before retry
                    urllib.request.urlretrieve(driver_url, duckdb_driver_path)
                    console.print(f"[green]✓[/green] Downloaded DuckDB driver ({duckdb_driver_path.stat().st_size // 1024 // 1024}MB)")
                    driver_downloaded = True
                    break
                except Exception as e:
                    if attempt == 2:  # Last attempt failed
                        break
                    continue

            if not driver_downloaded:
                print_error("✗ Failed to download DuckDB driver (network issue)")
                console.print("    [yellow]Don't worry![/yellow] The driver will be downloaded automatically when you run:")
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
        dbt_project_name = config.project.name.lower().replace(' ', '_').replace('-', '_')

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
        with open(dbt_project_path, 'w', encoding='utf-8') as f:
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
        with open(profiles_path, 'w', encoding='utf-8') as f:
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
        with open(macro_path, 'w', encoding='utf-8') as f:
            f.write(macro_content)

        print_success("Created dbt project files (dbt_project.yml, profiles.yml, macros)")

    def _generate_dbt_docs(self) -> bool:
        """
        Generate dbt documentation (works even for empty project)

        Returns:
            True if successful, False if generation failed
        """
        import subprocess
        import sys
        import shutil

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
                [dbt_cmd, "docs", "generate", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)],
                cwd=dbt_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                console.print("[green]✓[/green] dbt docs generated successfully")

                # Check that index.html was created
                index_path = dbt_dir / "target" / "index.html"
                if index_path.exists():
                    console.print("[green]✓[/green] Documentation available at dbt/target/index.html")
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
            "README.md",  # Only if created by us
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

    def _print_success_message(self, warnings=None, failures=None):
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

        console.print(Panel(
            message,
            title=title,
            border_style=border_style
        ))
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
