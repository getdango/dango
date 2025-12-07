"""
Dango Config Loader

Handles loading and validation of YAML configuration files.
"""

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import ValidationError

from .models import DangoConfig, ProjectContext, SourcesConfig
from .exceptions import ConfigError, ConfigNotFoundError, ConfigValidationError


class ConfigLoader:
    """Loads and validates Dango configuration"""

    DANGO_DIR = ".dango"
    PROJECT_FILE = "project.yml"
    SOURCES_FILE = "sources.yml"

    def __init__(self, project_root: Optional[Path] = None):
        """
        Initialize config loader.

        Args:
            project_root: Project root directory (defaults to current directory)
        """
        self.project_root = project_root or Path.cwd()
        self.dango_dir = self.project_root / self.DANGO_DIR
        self.project_file = self.dango_dir / self.PROJECT_FILE
        self.sources_file = self.dango_dir / self.SOURCES_FILE

    def is_dango_project(self) -> bool:
        """Check if current directory is a Dango project"""
        return self.dango_dir.exists() and self.project_file.exists()

    def find_project_root(self, start_path: Optional[Path] = None) -> Optional[Path]:
        """
        Find Dango project root by walking up directory tree.

        Args:
            start_path: Starting directory (defaults to current directory)

        Returns:
            Project root path or None if not found
        """
        current = start_path or Path.cwd()

        # Walk up directory tree
        for parent in [current] + list(current.parents):
            if (parent / self.DANGO_DIR / self.PROJECT_FILE).exists():
                return parent

        return None

    def load_yaml(self, file_path: Path) -> dict:
        """
        Load YAML file with error handling.

        Args:
            file_path: Path to YAML file

        Returns:
            Parsed YAML as dict

        Raises:
            ConfigNotFoundError: If file doesn't exist
            ConfigError: If YAML is invalid
        """
        if not file_path.exists():
            raise ConfigNotFoundError(
                f"Configuration file not found: {file_path}\n"
                f"Run 'dango init' to create a new project."
            )

        try:
            with open(file_path, 'r') as f:
                data = yaml.safe_load(f)
                return data or {}
        except yaml.YAMLError as e:
            raise ConfigError(
                f"Invalid YAML in {file_path}:\n{e}"
            )
        except Exception as e:
            raise ConfigError(
                f"Error reading {file_path}: {e}"
            )

    def save_yaml(self, data: dict, file_path: Path):
        """
        Save dict as YAML file atomically.

        Uses temp file + atomic rename to prevent data loss if write fails.

        Args:
            data: Data to save
            file_path: Output file path
        """
        # Ensure parent directory exists
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write to temp file first (atomic operation)
        temp_path = file_path.with_suffix(file_path.suffix + '.tmp')

        try:
            with open(temp_path, 'w') as f:
                yaml.safe_dump(
                    data,
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                    indent=2
                )

            # Atomic rename (overwrites destination on POSIX systems)
            temp_path.replace(file_path)

        except Exception as e:
            # Clean up temp file if it exists
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except:
                pass
            raise ConfigError(f"Error writing {file_path}: {e}")

    def load_project_context(self) -> ProjectContext:
        """
        Load project context from project.yml.

        Returns:
            ProjectContext model

        Raises:
            ConfigNotFoundError: If file doesn't exist
            ConfigValidationError: If validation fails
        """
        data = self.load_yaml(self.project_file)

        try:
            return ProjectContext(**data.get("project", {}))
        except ValidationError as e:
            raise ConfigValidationError(
                f"Invalid project configuration in {self.project_file}:\n{e}"
            )

    def load_sources_config(self) -> SourcesConfig:
        """
        Load sources configuration from sources.yml.

        Returns:
            SourcesConfig model

        Raises:
            ConfigNotFoundError: If file doesn't exist
            ConfigValidationError: If validation fails
        """
        # sources.yml is optional - return empty config if not found
        if not self.sources_file.exists():
            return SourcesConfig()

        data = self.load_yaml(self.sources_file)

        try:
            return SourcesConfig(**data)
        except ValidationError as e:
            raise ConfigValidationError(
                f"Invalid sources configuration in {self.sources_file}:\n{e}"
            )

    def load_config(self) -> DangoConfig:
        """
        Load complete Dango configuration.

        Returns:
            DangoConfig model

        Raises:
            ConfigNotFoundError: If project.yml doesn't exist
            ConfigValidationError: If validation fails
        """
        project = self.load_project_context()
        sources = self.load_sources_config()

        # Load platform settings from project.yml
        data = self.load_yaml(self.project_file)
        from dango.config.models import PlatformSettings
        platform = PlatformSettings(**data.get('platform', {}))

        return DangoConfig(
            project=project,
            sources=sources,
            platform=platform
        )

    def save_project_context(self, project: ProjectContext):
        """Save project context to project.yml"""
        # Check if project.yml exists and has platform settings
        existing_platform = {}
        if self.project_file.exists():
            existing_data = self.load_yaml(self.project_file)
            existing_platform = existing_data.get('platform', {})

        data = {
            "project": project.model_dump(mode="json", exclude_none=True),
            "platform": existing_platform  # Preserve existing platform settings
        }
        self.save_yaml(data, self.project_file)

    def save_sources_config(self, sources: SourcesConfig):
        """Save sources config to sources.yml"""
        data = sources.model_dump(mode="json", exclude_none=True)
        self.save_yaml(data, self.sources_file)

    def save_config(self, config: DangoConfig):
        """Save complete configuration"""
        # Save project context and platform settings together in project.yml
        data = {
            "project": config.project.model_dump(mode="json", exclude_none=True),
            "platform": config.platform.model_dump(mode="json", exclude_none=False)  # Include all fields
        }
        self.save_yaml(data, self.project_file)

        # Save sources separately
        self.save_sources_config(config.sources)

    def validate_config(self) -> tuple[bool, list[str]]:
        """
        Validate configuration files.

        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []

        try:
            self.load_config()
        except ConfigNotFoundError as e:
            errors.append(str(e))
        except ConfigValidationError as e:
            errors.append(str(e))
        except ConfigError as e:
            errors.append(str(e))

        return (len(errors) == 0, errors)


def get_config(project_root: Optional[Path] = None) -> DangoConfig:
    """
    Helper function to load config.

    Args:
        project_root: Project root directory (defaults to current directory)

    Returns:
        DangoConfig instance
    """
    loader = ConfigLoader(project_root)
    return loader.load_config()


def load_config(project_root: Optional[Path] = None) -> DangoConfig:
    """
    Alias for get_config - load configuration.

    Args:
        project_root: Project root directory (defaults to current directory)

    Returns:
        DangoConfig instance
    """
    return get_config(project_root)


def save_config(config: DangoConfig, project_root: Optional[Path] = None) -> None:
    """
    Helper function to save config.

    Args:
        config: DangoConfig instance to save
        project_root: Project root directory (defaults to current directory)
    """
    loader = ConfigLoader(project_root)
    loader.save_config(config)


def check_unreferenced_custom_sources(
    project_dir: Path,
    sources_config: SourcesConfig
) -> list[str]:
    """
    Find Python files in custom_sources/ that aren't referenced in sources.yml.

    This helps users who create custom dlt sources but forget to add
    the corresponding dlt_native entry to sources.yml.

    Args:
        project_dir: Project root directory
        sources_config: Loaded sources configuration

    Returns:
        List of unreferenced Python module names (without .py extension)
    """
    custom_sources_dir = project_dir / "custom_sources"
    if not custom_sources_dir.exists():
        return []

    # Get all .py files (excluding __init__.py and __pycache__)
    py_files = [
        f.stem for f in custom_sources_dir.glob("*.py")
        if f.name not in ("__init__.py",) and not f.name.startswith(".")
    ]

    # Get referenced modules from dlt_native sources
    referenced = set()
    for source in sources_config.sources:
        if source.type == "dlt_native" and source.dlt_native:
            referenced.add(source.dlt_native.source_module)

    # Return unreferenced modules
    return [f for f in py_files if f not in referenced]


def format_unreferenced_sources_warning(unreferenced: list[str]) -> str:
    """
    Format a helpful warning message for unreferenced custom sources.

    Args:
        unreferenced: List of unreferenced module names

    Returns:
        Formatted warning message with actionable instructions
    """
    if not unreferenced:
        return ""

    files_list = "\n".join(f"   - custom_sources/{f}.py" for f in unreferenced)
    example_name = unreferenced[0]

    return f"""
⚠️  Unreferenced custom sources detected:
{files_list}

These files won't be synced. To use them, add to .dango/sources.yml:

  - name: {example_name}
    type: dlt_native
    enabled: true
    dlt_native:
      source_module: {example_name}
      source_function: <function_name>
      function_kwargs: {{}}

Docs: https://docs.getdango.dev/custom-sources
"""
