"""tests/unit/test_exceptions.py

Tests for the unified exception hierarchy in dango/exceptions.py.
"""

import pytest

from dango.exceptions import (
    ConfigError,
    ConfigNotFoundError,
    ConfigValidationError,
    CSVSchemaMismatchError,
    DangoError,
    DbtLockError,
    DiskSpaceError,
    DuckDBHealthError,
    InfrastructureError,
    IngestionError,
    InvalidDateFormatError,
    InvalidPortError,
    InvalidSourceNameError,
    ProjectNotFoundError,
    SyncTimeoutError,
    ValidationError,
    WebAPIError,
    is_debug_mode,
)


@pytest.mark.unit
class TestDangoErrorBase:
    """Tests for DangoError base class."""

    def test_basic_construction(self):
        exc = DangoError("something broke")
        assert str(exc) == "something broke"
        assert exc.error_code == "DANGO-G000"
        assert exc.context == {}
        assert exc.user_message == "something broke"

    def test_explicit_error_code(self):
        exc = DangoError("oops", error_code="DANGO-X999")
        assert exc.error_code == "DANGO-X999"

    def test_context_dict(self):
        ctx = {"path": "/tmp/test", "size": 42}
        exc = DangoError("fail", context=ctx)
        assert exc.context == ctx

    def test_user_message_override(self):
        exc = DangoError("internal detail", user_message="Something went wrong.")
        assert exc.user_message == "Something went wrong."
        assert str(exc) == "internal detail"

    def test_user_message_defaults_to_message(self):
        exc = DangoError("the message")
        assert exc.user_message == "the message"

    def test_empty_message(self):
        exc = DangoError()
        assert str(exc) == ""
        assert exc.user_message == ""

    def test_catchable_as_exception(self):
        exc = DangoError("test")
        assert isinstance(exc, Exception)

    def test_repr(self):
        exc = DangoError("oops", error_code="DANGO-X001")
        assert repr(exc) == "DangoError('oops', error_code='DANGO-X001')"

    def test_explicit_empty_user_message(self):
        exc = DangoError("internal", user_message="")
        assert exc.user_message == ""
        assert str(exc) == "internal"


@pytest.mark.unit
class TestExceptionHierarchy:
    """Tests for inheritance relationships."""

    @pytest.mark.parametrize(
        "exc_class",
        [
            ConfigError,
            ConfigNotFoundError,
            ConfigValidationError,
            ProjectNotFoundError,
            IngestionError,
            SyncTimeoutError,
            CSVSchemaMismatchError,
            InfrastructureError,
            DiskSpaceError,
            DuckDBHealthError,
            DbtLockError,
            ValidationError,
            InvalidSourceNameError,
            InvalidDateFormatError,
            InvalidPortError,
            WebAPIError,
        ],
    )
    def test_all_inherit_from_dango_error(self, exc_class):
        assert issubclass(exc_class, DangoError)
        exc = exc_class("test")
        assert isinstance(exc, DangoError)

    def test_config_hierarchy(self):
        assert issubclass(ConfigNotFoundError, ConfigError)
        assert issubclass(ConfigValidationError, ConfigError)
        assert issubclass(ProjectNotFoundError, ConfigError)

    def test_ingestion_hierarchy(self):
        assert issubclass(SyncTimeoutError, IngestionError)
        assert issubclass(CSVSchemaMismatchError, IngestionError)

    def test_infrastructure_hierarchy(self):
        assert issubclass(DiskSpaceError, InfrastructureError)
        assert issubclass(DuckDBHealthError, InfrastructureError)
        assert issubclass(DbtLockError, InfrastructureError)

    def test_validation_hierarchy(self):
        assert issubclass(InvalidSourceNameError, ValidationError)
        assert issubclass(InvalidDateFormatError, ValidationError)
        assert issubclass(InvalidPortError, ValidationError)


@pytest.mark.unit
class TestDefaultErrorCodes:
    """Tests for _default_error_code class attributes."""

    @pytest.mark.parametrize(
        ("exc_class", "expected_code"),
        [
            (DangoError, "DANGO-G000"),
            (ConfigError, "DANGO-C001"),
            (ConfigNotFoundError, "DANGO-C002"),
            (ConfigValidationError, "DANGO-C003"),
            (ProjectNotFoundError, "DANGO-C004"),
            (IngestionError, "DANGO-I001"),
            (SyncTimeoutError, "DANGO-I002"),
            (CSVSchemaMismatchError, "DANGO-I003"),
            (InfrastructureError, "DANGO-U001"),
            (DiskSpaceError, "DANGO-U002"),
            (DuckDBHealthError, "DANGO-U003"),
            (DbtLockError, "DANGO-U004"),
            (ValidationError, "DANGO-V001"),
            (InvalidSourceNameError, "DANGO-V002"),
            (InvalidDateFormatError, "DANGO-V003"),
            (InvalidPortError, "DANGO-V004"),
            (WebAPIError, "DANGO-W001"),
        ],
    )
    def test_default_error_code(self, exc_class, expected_code):
        exc = exc_class("test")
        assert exc.error_code == expected_code

    def test_explicit_code_overrides_default(self):
        exc = ConfigError("test", error_code="DANGO-C999")
        assert exc.error_code == "DANGO-C999"


@pytest.mark.unit
class TestDbtLockErrorCompat:
    """Tests for DbtLockError backward compatibility."""

    def test_lock_info_param(self):
        info = {"pid": 123, "source": "cli"}
        exc = DbtLockError("locked", lock_info=info)
        assert exc.lock_info == info
        assert exc.error_code == "DANGO-U004"

    def test_lock_info_defaults_to_none(self):
        exc = DbtLockError("locked")
        assert exc.lock_info is None

    def test_lock_info_with_context(self):
        exc = DbtLockError("locked", lock_info={"pid": 1}, context={"op": "sync"})
        assert exc.lock_info == {"pid": 1}
        assert exc.context == {"op": "sync"}


@pytest.mark.unit
class TestIsDebugMode:
    """Tests for is_debug_mode() function."""

    def test_not_set(self, monkeypatch):
        monkeypatch.delenv("DANGO_DEBUG", raising=False)
        assert is_debug_mode() is False

    def test_set_to_1(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "1")
        assert is_debug_mode() is True

    def test_set_to_true(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "true")
        assert is_debug_mode() is True

    def test_set_to_yes(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "yes")
        assert is_debug_mode() is True

    def test_set_to_TRUE_uppercase(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "TRUE")
        assert is_debug_mode() is True

    def test_set_to_0(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "0")
        assert is_debug_mode() is False

    def test_set_to_empty(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "")
        assert is_debug_mode() is False

    def test_set_to_random_string(self, monkeypatch):
        monkeypatch.setenv("DANGO_DEBUG", "maybe")
        assert is_debug_mode() is False


@pytest.mark.unit
class TestBackwardCompatImports:
    """Tests that re-export shims return the same class objects."""

    def test_config_exceptions_reexport(self):
        from dango.config.exceptions import ConfigError as CE1
        from dango.config.exceptions import ConfigNotFoundError as CNFE1
        from dango.config.exceptions import ConfigValidationError as CVE1
        from dango.config.exceptions import ProjectNotFoundError as PNFE1

        assert CE1 is ConfigError
        assert CNFE1 is ConfigNotFoundError
        assert CVE1 is ConfigValidationError
        assert PNFE1 is ProjectNotFoundError

    def test_utils_dbt_lock_reexport(self):
        from dango.utils.dbt_lock import DbtLockError as DLE1

        assert DLE1 is DbtLockError

    def test_utils_init_reexport(self):
        from dango.utils import DbtLockError as DLE2

        assert DLE2 is DbtLockError
