"""tests/unit/test_config_exceptions.py

Tests for dango.config.exceptions — exception hierarchy.
"""

import pytest

from dango.config.exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    ProjectNotFoundError,
)


@pytest.mark.unit
class TestConfigExceptions:
    def test_config_error_is_exception(self):
        assert issubclass(ConfigError, Exception)

    def test_config_not_found_is_config_error(self):
        assert issubclass(ConfigNotFoundError, ConfigError)

    def test_config_validation_is_config_error(self):
        assert issubclass(ConfigValidationError, ConfigError)

    def test_project_not_found_is_config_error(self):
        assert issubclass(ProjectNotFoundError, ConfigError)

    def test_error_message_preserved(self):
        msg = "Something went wrong"
        err = ConfigError(msg)
        assert str(err) == msg
