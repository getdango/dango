"""Root test fixtures for the Dango test suite."""

import pytest
import yaml

from dango.config.models import (
    CSVSourceConfig,
    DangoConfig,
    DataSource,
    ProjectContext,
    SourcesConfig,
    SourceType,
)


@pytest.fixture
def sample_project_context():
    """A minimal valid ProjectContext instance (no I/O)."""
    return ProjectContext(
        name="Test Analytics",
        created_by="test@example.com",
        purpose="Unit testing the Dango config system",
    )


@pytest.fixture
def sample_sources_config():
    """A SourcesConfig with one CSV source (no I/O)."""
    return SourcesConfig(
        sources=[
            DataSource(
                name="test_csv",
                type=SourceType.CSV,
                csv=CSVSourceConfig(directory="/tmp/test_data"),
            )
        ]
    )


@pytest.fixture
def sample_config(sample_project_context, sample_sources_config):
    """A complete DangoConfig combining project context and sources (no I/O)."""
    return DangoConfig(
        project=sample_project_context,
        sources=sample_sources_config,
    )


@pytest.fixture
def tmp_project_dir(tmp_path):
    """Create a temporary Dango project directory with valid config files.

    Returns the project root path containing .dango/project.yml and .dango/sources.yml.
    """
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()

    project_data = {
        "project": {
            "name": "Test Project",
            "created_by": "test@example.com",
            "purpose": "Integration testing",
        }
    }
    with open(dango_dir / "project.yml", "w") as f:
        yaml.safe_dump(project_data, f, default_flow_style=False)

    sources_data = {
        "version": "1.0",
        "sources": [
            {
                "name": "test_csv",
                "type": "csv",
                "csv": {
                    "directory": str(tmp_path / "data"),
                },
            }
        ],
    }
    with open(dango_dir / "sources.yml", "w") as f:
        yaml.safe_dump(sources_data, f, default_flow_style=False)

    return tmp_path
