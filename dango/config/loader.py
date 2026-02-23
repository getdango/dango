"""dango/config/loader.py

Handles loading and validation of YAML configuration files.
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .exceptions import ConfigError, ConfigNotFoundError, ConfigValidationError
from .models import CloudConfig, DangoConfig, ProjectContext, SourcesConfig


class ConfigLoader:
    """Loads and validates Dango configuration"""

    DANGO_DIR = ".dango"
    PROJECT_FILE = "project.yml"
    SOURCES_FILE = "sources.yml"
    CLOUD_FILE = "cloud.yml"

    def __init__(self, project_root: Path | None = None):
        """
        Initialize config loader.

        Args:
            project_root: Project root directory (defaults to current directory)
        """
        self.project_root = project_root or Path.cwd()
        self.dango_dir = self.project_root / self.DANGO_DIR
        self.project_file = self.dango_dir / self.PROJECT_FILE
        self.sources_file = self.dango_dir / self.SOURCES_FILE
        self.cloud_file = self.dango_dir / self.CLOUD_FILE

    def is_dango_project(self) -> bool:
        """Check if current directory is a Dango project"""
        return self.dango_dir.exists() and self.project_file.exists()

    def find_project_root(self, start_path: Path | None = None) -> Path | None:
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
            with open(file_path) as f:
                data = yaml.safe_load(f)
                return data or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {file_path}:\n{e}") from e
        except Exception as e:
            raise ConfigError(f"Error reading {file_path}: {e}") from e

    def save_yaml(self, data: dict[str, Any], file_path: Path) -> None:
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
        temp_path = file_path.with_suffix(file_path.suffix + ".tmp")

        try:
            with open(temp_path, "w") as f:
                yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, indent=2)

            # Atomic rename (overwrites destination on POSIX systems)
            temp_path.replace(file_path)

        except Exception as e:
            # Clean up temp file if it exists
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except Exception:
                pass
            raise ConfigError(f"Error writing {file_path}: {e}") from e

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
            ) from e

    SUPPORTED_SOURCES_VERSIONS = {"1.0"}

    def load_sources_config(self) -> SourcesConfig:
        """
        Load sources configuration from sources.yml.

        Returns:
            SourcesConfig model

        Raises:
            ConfigNotFoundError: If file doesn't exist
            ConfigValidationError: If validation fails
            ConfigVersionError: If version is unsupported
        """
        # sources.yml is optional - return empty config if not found
        if not self.sources_file.exists():
            return SourcesConfig()

        data = self.load_yaml(self.sources_file)

        try:
            sources = SourcesConfig(**data)
        except ValidationError as e:
            raise ConfigValidationError(
                f"Invalid sources configuration in {self.sources_file}:\n{e}"
            ) from e

        if sources.version not in self.SUPPORTED_SOURCES_VERSIONS:
            from dango.exceptions import ConfigVersionError

            raise ConfigVersionError(
                f"sources.yml version '{sources.version}' is not supported. "
                f"Supported: {', '.join(sorted(self.SUPPORTED_SOURCES_VERSIONS))}. "
                f"Upgrade Dango to a version that supports this config format.",
                context={
                    "file": str(self.sources_file),
                    "version": sources.version,
                },
            )

        return sources

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

        # Load platform and auth settings from project.yml
        data = self.load_yaml(self.project_file)
        from dango.config.models import AuthConfig, PlatformSettings

        platform = PlatformSettings(**data.get("platform", {}))
        auth = AuthConfig(**data.get("auth", {}))

        return DangoConfig(project=project, sources=sources, platform=platform, auth=auth)

    def save_project_context(self, project: ProjectContext) -> None:
        """Save project context to project.yml"""
        # Check if project.yml exists and has platform/auth settings
        existing_platform = {}
        existing_auth = {}
        if self.project_file.exists():
            existing_data = self.load_yaml(self.project_file)
            existing_platform = existing_data.get("platform", {})
            existing_auth = existing_data.get("auth", {})

        data: dict[str, Any] = {
            "project": project.model_dump(mode="json", exclude_none=True),
            "platform": existing_platform,
        }
        if existing_auth:
            data["auth"] = existing_auth
        self.save_yaml(data, self.project_file)

    def save_sources_config(self, sources: SourcesConfig) -> None:
        """Save sources config to sources.yml"""
        data = sources.model_dump(mode="json", exclude_none=True)
        self.save_yaml(data, self.sources_file)

    def save_config(self, config: DangoConfig) -> None:
        """Save complete configuration"""
        from dango.config.models import AuthConfig

        # Save project context, platform, and auth settings together in project.yml
        data: dict[str, Any] = {
            "project": config.project.model_dump(mode="json", exclude_none=True),
            "platform": config.platform.model_dump(mode="json", exclude_none=False),
        }
        # Only write auth section if non-default to keep project.yml clean
        auth_data = config.auth.model_dump(mode="json", exclude_none=False)
        default_auth = AuthConfig().model_dump(mode="json", exclude_none=False)
        if auth_data != default_auth:
            data["auth"] = auth_data
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

        from dango.exceptions import ConfigVersionError

        try:
            self.load_config()
        except ConfigNotFoundError as e:
            errors.append(str(e))
        except ConfigValidationError as e:
            errors.append(str(e))
        except ConfigVersionError as e:
            errors.append(str(e))
        except ConfigError as e:
            errors.append(str(e))

        return (len(errors) == 0, errors)

    def load_cloud_config(self) -> CloudConfig | None:
        """
        Load cloud deployment config from .dango/cloud.yml.

        Returns:
            CloudConfig if the file exists and is valid, None if not deployed.

        Raises:
            ConfigValidationError: If the file exists but fails validation
        """
        if not self.cloud_file.exists():
            return None

        try:
            with open(self.cloud_file) as f:
                import yaml

                data: dict = yaml.safe_load(f) or {}
        except Exception as e:
            raise ConfigError(f"Error reading {self.cloud_file}: {e}") from e

        try:
            return CloudConfig(**data)
        except Exception as e:
            raise ConfigValidationError(
                f"Invalid cloud configuration in {self.cloud_file}:\n{e}"
            ) from e

    def save_cloud_config(self, cloud_config: CloudConfig) -> None:
        """
        Save cloud deployment config to .dango/cloud.yml.

        Args:
            cloud_config: CloudConfig model to persist
        """
        data = cloud_config.model_dump(mode="json", exclude_none=True)
        self.save_yaml(data, self.cloud_file)
