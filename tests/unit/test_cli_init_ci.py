"""tests/unit/test_cli_init_ci.py

Tests for CI workflow generation in dango init.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dango.cli.init import ProjectInitializer


@pytest.mark.unit
class TestCreateCiWorkflow:
    """Tests for ProjectInitializer._create_ci_workflow()."""

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
