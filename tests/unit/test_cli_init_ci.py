"""tests/unit/test_cli_init_ci.py

Tests for pre-commit config and CI workflow generation in dango init.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dango.cli.init import ProjectInitializer


@pytest.mark.unit
class TestCreateGitignore:
    """Tests for ProjectInitializer._create_gitignore()."""

    def test_new_gitignore_keeps_existing_and_sensitive_patterns(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_gitignore()

        content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
        assert ".dango/metabase.yml\n" in content
        for pattern in (
            ".dango/backups/",
            "dango-backup-*",
            ".dango/metabase.yml*",
            ".dango/credentials",
            ".dlt/.encryption_key",
        ):
            assert content.count(pattern) == 1

    def test_existing_dango_gitignore_gets_sensitive_artifact_patterns(
        self, tmp_path: Path
    ) -> None:
        gitignore_path = tmp_path / ".gitignore"
        gitignore_path.write_text("# Dango\n# preserve this user rule\ncustom-output/\n")

        initializer = ProjectInitializer(tmp_path)
        initializer._create_gitignore()

        content = gitignore_path.read_text(encoding="utf-8")
        assert "# preserve this user rule\ncustom-output/\n" in content
        for pattern in (
            ".dango/backups/",
            "dango-backup-*",
            ".dango/metabase.yml*",
            ".dango/credentials",
            ".dlt/.encryption_key",
        ):
            assert content.count(pattern) == 1


@pytest.mark.unit
class TestCreatePreCommitConfig:
    """Tests for ProjectInitializer._create_pre_commit_config()."""

    def test_creates_config_file(self, tmp_path: Path) -> None:
        """_create_pre_commit_config creates the expected file."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_pre_commit_config()
        cfg = tmp_path / ".pre-commit-config.yaml"
        assert cfg.exists()

    def test_config_is_valid_yaml(self, tmp_path: Path) -> None:
        """Generated config parses as valid YAML."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_pre_commit_config()
        cfg = tmp_path / ".pre-commit-config.yaml"
        data = yaml.safe_load(cfg.read_text())
        assert data is not None
        assert "repos" in data
        assert len(data["repos"]) == 2  # ruff + local

    def test_config_has_ruff_hooks(self, tmp_path: Path) -> None:
        """Config includes ruff and ruff-format hooks."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_pre_commit_config()
        content = (tmp_path / ".pre-commit-config.yaml").read_text()
        assert "ruff" in content
        assert "ruff-format" in content
        assert "ruff-pre-commit" in content

    def test_config_has_dango_validate(self, tmp_path: Path) -> None:
        """Config includes dango config validate hook."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_pre_commit_config()
        content = (tmp_path / ".pre-commit-config.yaml").read_text()
        assert "dango config validate" in content
        assert "dango-validate" in content

    def test_config_has_secrets_check(self, tmp_path: Path) -> None:
        """Config includes no-secrets hook."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_pre_commit_config()
        content = (tmp_path / ".pre-commit-config.yaml").read_text()
        assert "no-secrets" in content
        assert "secrets" in content
        assert ".env" in content


@pytest.mark.unit
class TestCreateCiWorkflow:
    """Tests for ProjectInitializer._create_ci_workflow() (opt-in)."""

    def test_creates_workflow_file(self, tmp_path: Path) -> None:
        """_create_ci_workflow creates the expected file."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_ci_workflow()
        wf = tmp_path / ".github" / "workflows" / "dango-validate.yml"
        assert wf.exists()

    def test_workflow_is_valid_yaml(self, tmp_path: Path) -> None:
        """Generated workflow parses as valid YAML."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_ci_workflow()
        wf = tmp_path / ".github" / "workflows" / "dango-validate.yml"
        data = yaml.safe_load(wf.read_text())
        assert data is not None
        assert data["name"] == "Dango Validate"
        assert "jobs" in data

    def test_workflow_commands(self, tmp_path: Path) -> None:
        """Workflow uses dango config validate (not full validate) and dbt parse."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_ci_workflow()
        content = (tmp_path / ".github" / "workflows" / "dango-validate.yml").read_text()
        assert "dango config validate" in content
        assert "dbt parse --profiles-dir ." in content
        assert "pip install getdango" in content

    def test_idempotent_on_existing_directory(self, tmp_path: Path) -> None:
        """Works when .github/workflows/ already exists."""
        (tmp_path / ".github" / "workflows").mkdir(parents=True)
        initializer = ProjectInitializer(tmp_path)
        initializer._create_ci_workflow()
        assert (tmp_path / ".github" / "workflows" / "dango-validate.yml").exists()
