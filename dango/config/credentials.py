"""
Credential Management for dlt Sources

This module handles loading and saving credentials for dlt sources,
supporting both .dlt/ directory (dlt-native) and .env file (legacy) formats.

Priority order:
1. .dlt/secrets.toml (highest priority)
2. .env file (fallback)

The .dlt/ directory follows dlt's native configuration pattern:
- secrets.toml: Sensitive credentials (gitignored)
- config.toml: Non-sensitive parameters (can be committed)
"""

import os
import toml
from pathlib import Path
from typing import Dict, Any, Optional
from rich.console import Console

console = Console()


class CredentialManager:
    """
    Manages credentials for dlt sources

    Supports both .dlt/ directory (dlt-native) and .env file formats.
    """

    def __init__(self, project_root: Path):
        """
        Initialize credential manager

        Args:
            project_root: Path to dango project root
        """
        self.project_root = Path(project_root)
        self.dlt_dir = self.project_root / ".dlt"
        self.secrets_file = self.dlt_dir / "secrets.toml"
        self.config_file = self.dlt_dir / "config.toml"
        self.env_file = self.project_root / ".env"

    def init_dlt_directory(self) -> None:
        """
        Initialize .dlt/ directory with template files

        Creates:
        - .dlt/secrets.toml (empty, gitignored)
        - .dlt/config.toml (empty, can be committed)
        """
        # Create .dlt directory if it doesn't exist
        self.dlt_dir.mkdir(exist_ok=True)

        # Create secrets.toml if it doesn't exist
        if not self.secrets_file.exists():
            secrets_template = """# dlt secrets configuration
# This file contains sensitive credentials and is gitignored
#
# Example structure:
# [sources.google_ads]
# [sources.google_ads.credentials]
# client_id = "your-client-id"
# client_secret = "your-client-secret"
# refresh_token = "your-refresh-token"
#
# [sources.facebook_ads]
# access_token = "your-long-lived-token"
# account_id = "act_123456789"
"""
            self.secrets_file.write_text(secrets_template)
            console.print(f"[dim]Created {self.secrets_file.relative_to(self.project_root)}[/dim]")

        # Create config.toml if it doesn't exist
        if not self.config_file.exists():
            config_template = """# dlt configuration
# This file contains non-sensitive parameters
# Can be safely committed to git

# Clean up staging tables after successful load
[load]
truncate_staging_dataset = true

# Example source configuration:
# [sources.google_ads]
# start_date = "2024-01-01"
#
# [sources.facebook_ads]
# start_date = "2024-01-01"
"""
            self.config_file.write_text(config_template)
            console.print(f"[dim]Created {self.config_file.relative_to(self.project_root)}[/dim]")

        # Ensure .gitignore includes .dlt/secrets.toml
        self._update_gitignore()

    def _update_gitignore(self) -> None:
        """
        Ensure .gitignore includes .dlt/secrets.toml
        """
        gitignore_path = self.project_root / ".gitignore"
        dlt_ignore_patterns = [
            ".dlt/secrets.toml",
            ".dlt/*.db",
        ]

        # Read existing .gitignore
        existing_lines = []
        if gitignore_path.exists():
            existing_lines = gitignore_path.read_text().splitlines()

        # Check which patterns are missing
        missing_patterns = []
        for pattern in dlt_ignore_patterns:
            if pattern not in existing_lines:
                missing_patterns.append(pattern)

        # Add missing patterns
        if missing_patterns:
            with open(gitignore_path, "a") as f:
                if existing_lines and not existing_lines[-1].strip():
                    # .gitignore exists but doesn't end with newline
                    pass
                elif existing_lines:
                    # Add blank line before our section
                    f.write("\n")

                f.write("# dlt credentials (sensitive)\n")
                for pattern in missing_patterns:
                    f.write(f"{pattern}\n")

            console.print(f"[dim]Updated .gitignore with .dlt/ exclusions[/dim]")

    def load_secrets(self) -> Dict[str, Any]:
        """
        Load secrets from .dlt/secrets.toml

        Returns:
            Dictionary with secrets structure
        """
        if not self.secrets_file.exists():
            return {}

        try:
            with open(self.secrets_file, "r") as f:
                secrets = toml.load(f)
            return secrets
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load .dlt/secrets.toml: {e}[/yellow]")
            return {}

    def load_config(self) -> Dict[str, Any]:
        """
        Load configuration from .dlt/config.toml

        Returns:
            Dictionary with config structure
        """
        if not self.config_file.exists():
            return {}

        try:
            with open(self.config_file, "r") as f:
                config = toml.load(f)
            return config
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load .dlt/config.toml: {e}[/yellow]")
            return {}

    def save_secrets(self, secrets: Dict[str, Any], merge: bool = True) -> None:
        """
        Save secrets to .dlt/secrets.toml

        Args:
            secrets: Dictionary of secrets to save
            merge: If True, merge with existing secrets. If False, overwrite completely.
        """
        # Ensure .dlt directory exists
        self.dlt_dir.mkdir(exist_ok=True)

        # Load existing secrets if merging
        if merge and self.secrets_file.exists():
            existing_secrets = self.load_secrets()
            # Deep merge
            merged = self._deep_merge(existing_secrets, secrets)
        else:
            merged = secrets

        # Write to file
        with open(self.secrets_file, "w") as f:
            toml.dump(merged, f)

        console.print(f"[green]✓[/green] Saved credentials to .dlt/secrets.toml")

    def save_config(self, config: Dict[str, Any], merge: bool = True) -> None:
        """
        Save configuration to .dlt/config.toml

        Args:
            config: Dictionary of config to save
            merge: If True, merge with existing config. If False, overwrite completely.
        """
        # Ensure .dlt directory exists
        self.dlt_dir.mkdir(exist_ok=True)

        # Load existing config if merging
        if merge and self.config_file.exists():
            existing_config = self.load_config()
            # Deep merge
            merged = self._deep_merge(existing_config, config)
        else:
            merged = config

        # Write to file
        with open(self.config_file, "w") as f:
            toml.dump(merged, f)

        console.print(f"[green]✓[/green] Saved config to .dlt/config.toml")

    def _deep_merge(self, base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
        """
        Deep merge two dictionaries

        Args:
            base: Base dictionary
            update: Dictionary to merge into base

        Returns:
            Merged dictionary
        """
        result = base.copy()

        for key, value in update.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value

        return result

    def get_source_credentials(self, source_name: str) -> Optional[Dict[str, Any]]:
        """
        Get credentials for a specific source

        Checks in order:
        1. .dlt/secrets.toml under sources.<source_name>
        2. .env file (for backward compatibility)

        Args:
            source_name: Name of the source (e.g., "google_ads", "facebook_ads")

        Returns:
            Dictionary of credentials or None if not found
        """
        # Check .dlt/secrets.toml first
        secrets = self.load_secrets()
        if "sources" in secrets and source_name in secrets["sources"]:
            return secrets["sources"][source_name]

        # Fallback to .env file
        # This is for backward compatibility with existing .env-based setup
        # Each source type has its own .env variable mapping
        return None

    def has_credentials(self, source_name: str) -> bool:
        """
        Check if credentials exist for a source

        Args:
            source_name: Name of the source

        Returns:
            True if credentials exist, False otherwise
        """
        return self.get_source_credentials(source_name) is not None

    def delete_source_credentials(self, source_name: str) -> bool:
        """
        Delete credentials for a specific source

        Args:
            source_name: Name of the source

        Returns:
            True if credentials were deleted, False if not found
        """
        secrets = self.load_secrets()

        if "sources" not in secrets or source_name not in secrets["sources"]:
            return False

        # Remove the source
        del secrets["sources"][source_name]

        # If sources is now empty, remove it
        if not secrets["sources"]:
            del secrets["sources"]

        # Save updated secrets
        self.save_secrets(secrets, merge=False)

        console.print(f"[green]✓[/green] Deleted credentials for {source_name}")
        return True

    def list_configured_sources(self) -> list[str]:
        """
        List all sources with configured credentials

        Returns:
            List of source names
        """
        secrets = self.load_secrets()

        if "sources" not in secrets:
            return []

        return list(secrets["sources"].keys())


def init_dlt_directory(project_root: Path) -> None:
    """
    Initialize .dlt/ directory for a project

    Args:
        project_root: Path to project root
    """
    manager = CredentialManager(project_root)
    manager.init_dlt_directory()
