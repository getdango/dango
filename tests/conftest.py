"""tests/conftest.py

Root test fixtures for the Dango test suite.
"""

import logging
import os
from pathlib import Path

import pytest
import yaml

from tests.factories.config_factories import (
    make_dango_config,
    make_project_context,
    make_sources_config,
)

_session_id = f"{Path.cwd().name}:{os.getpid()}"


def pytest_sessionstart(session):
    logger = logging.getLogger("pytest.session")
    logger.info("=" * 60)
    logger.info(f"SESSION START [{_session_id}]")


def pytest_sessionfinish(session, exitstatus):
    logger = logging.getLogger("pytest.session")
    logger.info(f"SESSION END [{_session_id}] exit={exitstatus}")


def pytest_runtest_logstart(nodeid, location):
    logging.getLogger("pytest.test").info(f"[{_session_id}] START {nodeid}")


def pytest_runtest_logfinish(nodeid, location):
    logging.getLogger("pytest.test").info(f"[{_session_id}] END {nodeid}")


@pytest.fixture
def sample_project_context():
    """A minimal valid ProjectContext instance (no I/O)."""
    return make_project_context()


@pytest.fixture
def sample_sources_config():
    """A SourcesConfig with one CSV source (no I/O)."""
    return make_sources_config()


@pytest.fixture
def sample_config(sample_project_context, sample_sources_config):
    """A complete DangoConfig combining project context and sources (no I/O)."""
    return make_dango_config(
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
