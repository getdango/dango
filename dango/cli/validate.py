"""
Project Validation

Validates Dango project configuration and resources.

Created: MVP Week 1 Day 5 (Oct 27, 2025)
"""

from pathlib import Path
from typing import Dict, List, Any, Optional
import importlib.util
import subprocess

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from dango.config import ConfigLoader

console = Console()


class ValidationResult:
    """Result of a validation check"""

    def __init__(self, name: str, status: str, message: str = ""):
        """
        Initialize validation result

        Args:
            name: Name of check
            status: Status (pass, warn, fail)
            message: Additional message
        """
        self.name = name
        self.status = status  # pass, warn, fail
        self.message = message

    def __repr__(self):
        return f"ValidationResult({self.name}, {self.status})"


class ProjectValidator:
    """Validates Dango project configuration and setup"""

    def __init__(self, project_root: Path):
        """
        Initialize validator

        Args:
            project_root: Path to project root
        """
        self.project_root = project_root
        self.results: List[ValidationResult] = []

    def validate_all(self) -> Dict[str, Any]:
        """
        Run all validation checks

        Returns:
            Validation summary
        """
        console.print("[bold cyan]🔍 Validating Dango Project[/bold cyan]\n")

        # Run all checks
        self._check_project_structure()
        self._check_config_files()
        self._check_data_sources()
        self._check_custom_sources()  # Check for unreferenced custom sources
        self._check_oauth_credentials()  # NEW: OAuth validation
        self._check_dbt_setup()
        self._check_database()
        self._check_dependencies()
        self._check_permissions()

        # Summarize results
        summary = self._create_summary()
        self._display_results()

        return summary

    def _check_project_structure(self):
        """Check if project directory structure is correct"""
        required_dirs = [
            ".dango",
            "data",
            "dbt",
            "dbt/models",
            "dbt/models/staging",
        ]

        for dir_path in required_dirs:
            full_path = self.project_root / dir_path
            if full_path.exists():
                self.results.append(ValidationResult(
                    f"Directory: {dir_path}",
                    "pass"
                ))
            else:
                self.results.append(ValidationResult(
                    f"Directory: {dir_path}",
                    "warn",
                    f"Missing directory: {dir_path}"
                ))

    def _check_config_files(self):
        """Check if configuration files exist and are valid"""
        # Check project.yml
        project_yml = self.project_root / ".dango" / "project.yml"
        if project_yml.exists():
            try:
                loader = ConfigLoader(self.project_root)
                config = loader.load_config()

                self.results.append(ValidationResult(
                    "Config: project.yml",
                    "pass",
                    f"Project: {config.project.name}"
                ))
            except Exception as e:
                self.results.append(ValidationResult(
                    "Config: project.yml",
                    "fail",
                    f"Invalid YAML: {str(e)[:100]}"
                ))
        else:
            self.results.append(ValidationResult(
                "Config: project.yml",
                "fail",
                "File not found. Run 'dango init' first."
            ))

        # Check sources.yml
        sources_yml = self.project_root / ".dango" / "sources.yml"
        if sources_yml.exists():
            try:
                loader = ConfigLoader(self.project_root)
                config = loader.load_config()
                sources = config.sources.sources  # Direct attribute, not a method

                self.results.append(ValidationResult(
                    "Config: sources.yml",
                    "pass",
                    f"{len(sources)} source(s) configured"
                ))
            except Exception as e:
                self.results.append(ValidationResult(
                    "Config: sources.yml",
                    "fail",
                    f"Invalid YAML: {str(e)[:100]}"
                ))
        else:
            self.results.append(ValidationResult(
                "Config: sources.yml",
                "warn",
                "No sources configured. Run 'dango source add' to add sources."
            ))

    def _check_data_sources(self):
        """Check if data sources are properly configured"""
        try:
            loader = ConfigLoader(self.project_root)
            config = loader.load_config()
            sources = config.sources.get_enabled_sources()

            if not sources:
                self.results.append(ValidationResult(
                    "Data Sources",
                    "warn",
                    "No enabled sources found"
                ))
                return

            for source in sources:
                # Check if source has required parameters
                # Note: Most validation happens during source add/wizard
                # This just checks basic structure

                # Just report that source exists and is configured
                self.results.append(ValidationResult(
                    f"Source: {source.name}",
                    "pass",
                    f"Type: {source.type.value}"
                ))

        except Exception as e:
            self.results.append(ValidationResult(
                "Data Sources",
                "fail",
                f"Error checking sources: {str(e)[:100]}"
            ))

    def _check_custom_sources(self):
        """Check for unreferenced Python files in custom_sources/"""
        from dango.config.loader import check_unreferenced_custom_sources

        try:
            loader = ConfigLoader(self.project_root)
            config = loader.load_config()

            unreferenced = check_unreferenced_custom_sources(
                self.project_root,
                config.sources
            )

            if unreferenced:
                files_list = ", ".join(f"{f}.py" for f in unreferenced)
                self.results.append(ValidationResult(
                    "Custom Sources",
                    "warn",
                    f"Unreferenced files in custom_sources/: {files_list}. "
                    "Add dlt_native entries to sources.yml to use them."
                ))
            else:
                # Only report if custom_sources dir exists
                custom_sources_dir = self.project_root / "custom_sources"
                if custom_sources_dir.exists():
                    py_files = list(custom_sources_dir.glob("*.py"))
                    py_files = [f for f in py_files if f.name != "__init__.py"]
                    if py_files:
                        self.results.append(ValidationResult(
                            "Custom Sources",
                            "pass",
                            "All custom sources are referenced in sources.yml"
                        ))

        except Exception as e:
            self.results.append(ValidationResult(
                "Custom Sources",
                "fail",
                f"Error checking custom sources: {str(e)[:100]}"
            ))

    def _check_oauth_credentials(self):
        """Check OAuth credentials for configured sources"""
        from dango.oauth.storage import OAuthStorage
        from dango.ingestion.sources.registry import get_source_metadata, AuthType

        try:
            loader = ConfigLoader(self.project_root)
            config = loader.load_config()
            sources = config.sources.sources  # All sources (enabled and disabled)

            storage = OAuthStorage(self.project_root)

            # Filter to OAuth sources only
            oauth_sources = [
                s for s in sources
                if get_source_metadata(s.type.value).get("auth_type") == AuthType.OAUTH
            ]

            if not oauth_sources:
                # No OAuth sources configured - skip check
                return

            for source in oauth_sources:
                source_type = source.type.value
                source_name = source.name

                # Check if OAuth credentials exist
                oauth_cred = storage.get(source_type)

                if not oauth_cred:
                    # OAuth source but no credentials found
                    self.results.append(ValidationResult(
                        f"OAuth: {source_name}",
                        "fail",
                        f"No OAuth credentials found. Run 'dango auth {source_type}'"
                    ))
                    continue

                # Check if credentials are expired
                if oauth_cred.is_expired():
                    self.results.append(ValidationResult(
                        f"OAuth: {source_name}",
                        "fail",
                        f"Token expired on {oauth_cred.expires_at.strftime('%Y-%m-%d')}. Run 'dango auth {source_type}'"
                    ))
                elif oauth_cred.is_expiring_soon(days=7):
                    # Expiring soon - warning but not failure
                    days_left = oauth_cred.days_until_expiry()
                    self.results.append(ValidationResult(
                        f"OAuth: {source_name}",
                        "warn",
                        f"Token expires in {days_left} day(s) on {oauth_cred.expires_at.strftime('%Y-%m-%d')}"
                    ))
                else:
                    # Valid OAuth credentials
                    if oauth_cred.expires_at:
                        days_left = oauth_cred.days_until_expiry()
                        self.results.append(ValidationResult(
                            f"OAuth: {source_name}",
                            "pass",
                            f"Valid ({days_left} days until expiry)"
                        ))
                    else:
                        self.results.append(ValidationResult(
                            f"OAuth: {source_name}",
                            "pass",
                            "Valid (no expiry)"
                        ))

        except Exception as e:
            self.results.append(ValidationResult(
                "OAuth Credentials",
                "fail",
                f"Error checking OAuth: {str(e)[:100]}"
            ))

    def _check_dbt_setup(self):
        """Check if dbt is properly configured"""
        # Check dbt_project.yml
        dbt_project_yml = self.project_root / "dbt" / "dbt_project.yml"
        if dbt_project_yml.exists():
            self.results.append(ValidationResult(
                "dbt: dbt_project.yml",
                "pass"
            ))
        else:
            self.results.append(ValidationResult(
                "dbt: dbt_project.yml",
                "fail",
                "File not found"
            ))

        # Check profiles.yml
        profiles_yml = self.project_root / "dbt" / "profiles.yml"
        if profiles_yml.exists():
            self.results.append(ValidationResult(
                "dbt: profiles.yml",
                "pass"
            ))
        else:
            self.results.append(ValidationResult(
                "dbt: profiles.yml",
                "fail",
                "File not found"
            ))

        # Check if dbt models exist
        staging_dir = self.project_root / "dbt" / "models" / "staging"
        if staging_dir.exists():
            staging_models = list(staging_dir.glob("*.sql"))
            if staging_models:
                self.results.append(ValidationResult(
                    "dbt: Staging models",
                    "pass",
                    f"{len(staging_models)} model(s) found"
                ))
            else:
                self.results.append(ValidationResult(
                    "dbt: Staging models",
                    "warn",
                    "No staging models found. Run 'dango sync' to auto-generate."
                ))

        # Validate dbt models by running dbt parse
        self._validate_dbt_models()

    def _validate_dbt_models(self):
        """Validate dbt models by running dbt parse"""
        dbt_dir = self.project_root / "dbt"

        if not dbt_dir.exists():
            return

        try:
            # Run dbt parse to validate all models
            result = subprocess.run(
                ["dbt", "parse", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                # Parse successful - count model files directly for accurate count
                staging_dir = dbt_dir / "models" / "staging"
                intermediate_dir = dbt_dir / "models" / "intermediate"
                marts_dir = dbt_dir / "models" / "marts"

                model_count = 0
                for models_dir in [staging_dir, intermediate_dir, marts_dir]:
                    if models_dir.exists():
                        model_count += len(list(models_dir.glob("*.sql")))

                if model_count > 0:
                    self.results.append(ValidationResult(
                        "dbt: Model validation",
                        "pass",
                        f"All {model_count} model(s) validated successfully"
                    ))
                else:
                    self.results.append(ValidationResult(
                        "dbt: Model validation",
                        "pass",
                        "No models to validate (run 'dango sync' to generate)"
                    ))
            else:
                # Parse failed - extract error message
                error_output = result.stderr or result.stdout

                # Try to extract meaningful error
                error_lines = error_output.split('\n')
                error_msg = "Syntax errors found"

                # Look for specific error patterns
                for line in error_lines:
                    if 'ERROR' in line or 'Error' in line:
                        # Extract the actual error message
                        error_msg = line.strip()
                        break

                self.results.append(ValidationResult(
                    "dbt: Model validation",
                    "fail",
                    f"{error_msg}. Run 'dbt parse' for details."
                ))

        except subprocess.TimeoutExpired:
            self.results.append(ValidationResult(
                "dbt: Model validation",
                "warn",
                "Validation timed out (>60s). Models may be complex."
            ))
        except FileNotFoundError:
            self.results.append(ValidationResult(
                "dbt: Model validation",
                "fail",
                "dbt command not found. Ensure dbt is installed."
            ))
        except Exception as e:
            self.results.append(ValidationResult(
                "dbt: Model validation",
                "warn",
                f"Could not validate models: {str(e)[:100]}"
            ))

    def _check_database(self):
        """Check if DuckDB database exists and is accessible"""
        db_path = self.project_root / "data" / "warehouse.duckdb"

        if db_path.exists():
            try:
                import duckdb
                con = duckdb.connect(str(db_path), read_only=True)

                # Check if database has tables across ALL schemas (not just main)
                # Query information_schema to get all user tables
                result = con.execute("""
                    SELECT table_schema, COUNT(*) as table_count
                    FROM information_schema.tables
                    WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
                      AND table_type = 'BASE TABLE'
                      AND table_name NOT LIKE '_dlt_%'
                    GROUP BY table_schema
                """).fetchall()
                con.close()

                # Count total tables and format schema summary
                total_tables = sum(row[1] for row in result)
                schema_summary = ", ".join(f"{row[0]}: {row[1]}" for row in result if row[1] > 0)

                if total_tables > 0:
                    self.results.append(ValidationResult(
                        "Database: DuckDB",
                        "pass",
                        f"{total_tables} table(s) found ({schema_summary})"
                    ))
                else:
                    self.results.append(ValidationResult(
                        "Database: DuckDB",
                        "warn",
                        "Database is empty. Run 'dango sync' to load data."
                    ))

            except Exception as e:
                self.results.append(ValidationResult(
                    "Database: DuckDB",
                    "fail",
                    f"Cannot open database: {str(e)[:100]}"
                ))
        else:
            self.results.append(ValidationResult(
                "Database: DuckDB",
                "warn",
                "Database not found. Run 'dango sync' to create."
            ))

    def _check_dependencies(self):
        """Check if required Python packages are installed"""
        required_packages = [
            ("duckdb", "DuckDB"),
            ("dlt", "dlt"),
            ("dbt.adapters.duckdb", "dbt-duckdb"),  # Module name is different from package name
            ("rich", "Rich"),
            ("click", "Click"),
            ("pydantic", "Pydantic"),
            ("jinja2", "Jinja2"),
            ("watchdog", "Watchdog"),
        ]

        for module_name, display_name in required_packages:
            try:
                spec = importlib.util.find_spec(module_name)
                if spec is not None:
                    self.results.append(ValidationResult(
                        f"Package: {display_name}",
                        "pass"
                    ))
                else:
                    self.results.append(ValidationResult(
                        f"Package: {display_name}",
                        "fail",
                        f"Package not installed: {display_name}"
                    ))
            except Exception:
                self.results.append(ValidationResult(
                    f"Package: {display_name}",
                    "fail",
                    f"Package not installed: {display_name}"
                ))

        # Check dbt CLI
        try:
            result = subprocess.run(
                ["dbt", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Extract version from output
                version_line = result.stdout.split('\n')[0]
                self.results.append(ValidationResult(
                    "CLI: dbt",
                    "pass",
                    version_line
                ))
            else:
                self.results.append(ValidationResult(
                    "CLI: dbt",
                    "fail",
                    "dbt command failed"
                ))
        except FileNotFoundError:
            self.results.append(ValidationResult(
                "CLI: dbt",
                "fail",
                "dbt not found in PATH"
            ))
        except Exception as e:
            self.results.append(ValidationResult(
                "CLI: dbt",
                "fail",
                f"Error checking dbt: {str(e)[:100]}"
            ))

    def _check_permissions(self):
        """Check if directories are writable"""
        check_dirs = [
            "data",
            "dbt/models",
            ".dango",
        ]

        for dir_path in check_dirs:
            full_path = self.project_root / dir_path
            if full_path.exists():
                # Try to create a test file
                test_file = full_path / ".dango_test_write"
                try:
                    test_file.touch()
                    test_file.unlink()
                    self.results.append(ValidationResult(
                        f"Permissions: {dir_path}",
                        "pass"
                    ))
                except Exception:
                    self.results.append(ValidationResult(
                        f"Permissions: {dir_path}",
                        "fail",
                        "Directory is not writable"
                    ))

    def _create_summary(self) -> Dict[str, Any]:
        """Create validation summary"""
        pass_count = sum(1 for r in self.results if r.status == "pass")
        warn_count = sum(1 for r in self.results if r.status == "warn")
        fail_count = sum(1 for r in self.results if r.status == "fail")

        return {
            "total": len(self.results),
            "pass": pass_count,
            "warn": warn_count,
            "fail": fail_count,
            "results": self.results,
            "is_valid": fail_count == 0
        }

    def _display_results(self):
        """Display validation results in a table"""
        # Group by category
        categories = {}
        for result in self.results:
            category = result.name.split(":")[0] if ":" in result.name else "General"
            if category not in categories:
                categories[category] = []
            categories[category].append(result)

        # Display each category
        for category, results in categories.items():
            console.print(f"\n[bold]{category}[/bold]")

            table = Table(show_header=True, header_style="bold cyan", box=None)
            table.add_column("Check", style="white")
            table.add_column("Status", style="white")
            table.add_column("Details", style="dim")

            for result in results:
                # Status emoji and color
                if result.status == "pass":
                    status_str = "[green]✓ PASS[/green]"
                elif result.status == "warn":
                    status_str = "[yellow]⚠ WARN[/yellow]"
                else:
                    status_str = "[red]✗ FAIL[/red]"

                # Clean name (remove category prefix)
                clean_name = result.name.split(": ", 1)[1] if ": " in result.name else result.name

                table.add_row(
                    clean_name,
                    status_str,
                    result.message
                )

            console.print(table)

        # Overall summary
        summary = self._create_summary()

        console.print()
        if summary["is_valid"]:
            console.print(Panel(
                f"[green]✓ Project is valid![/green]\n\n"
                f"Passed: {summary['pass']}\n"
                f"Warnings: {summary['warn']}\n"
                f"Failed: {summary['fail']}",
                title="Validation Summary",
                border_style="green"
            ))
        else:
            console.print(Panel(
                f"[red]✗ Project has issues[/red]\n\n"
                f"Passed: {summary['pass']}\n"
                f"Warnings: {summary['warn']}\n"
                f"Failed: {summary['fail']}\n\n"
                f"[dim]Fix the failed checks above and run 'dango validate' again.[/dim]",
                title="Validation Summary",
                border_style="red"
            ))


def validate_project(project_root: Path) -> Dict[str, Any]:
    """
    Validate a Dango project

    Args:
        project_root: Path to project root

    Returns:
        Validation summary
    """
    validator = ProjectValidator(project_root)
    return validator.validate_all()
