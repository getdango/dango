"""Test factories for creating valid model instances and shared mocks.

Usage:
    from tests.factories.config_factories import make_project_context, make_dango_config
    from tests.factories.cloud_factories import make_ssh_mock, make_ssh_mock_configurable

    ctx = make_project_context(name="Custom Name")
    config = make_dango_config()
    ssh = make_ssh_mock()
"""

from tests.factories.cloud_factories import (
    make_ssh_mock,
    make_ssh_mock_configurable,
)
from tests.factories.config_factories import (
    make_csv_source_config,
    make_dango_config,
    make_data_source,
    make_google_sheets_source_config,
    make_platform_settings,
    make_project_context,
    make_sources_config,
    make_stakeholder,
)

__all__ = [
    "make_csv_source_config",
    "make_dango_config",
    "make_data_source",
    "make_google_sheets_source_config",
    "make_platform_settings",
    "make_project_context",
    "make_sources_config",
    "make_ssh_mock",
    "make_ssh_mock_configurable",
    "make_stakeholder",
]
