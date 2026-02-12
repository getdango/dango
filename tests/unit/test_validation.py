"""tests/unit/test_validation.py

Tests for input validation utilities in dango/validation.py.
"""

from datetime import datetime

import pytest

from dango.exceptions import (
    InvalidDateFormatError,
    InvalidPortError,
    InvalidSourceNameError,
)
from dango.validation import (
    sanitize_path_component,
    validate_date_string,
    validate_limit,
    validate_port_range,
    validate_source_name,
)


@pytest.mark.unit
class TestValidateSourceName:
    """Tests for validate_source_name()."""

    def test_valid_lowercase(self):
        assert validate_source_name("my_source") == "my_source"

    def test_valid_uppercase(self):
        assert validate_source_name("MySource") == "MySource"

    def test_valid_with_numbers(self):
        assert validate_source_name("source_123") == "source_123"

    def test_valid_single_char(self):
        assert validate_source_name("s") == "s"

    def test_valid_underscores(self):
        assert validate_source_name("my__source") == "my__source"

    def test_empty_string(self):
        with pytest.raises(InvalidSourceNameError, match="must not be empty"):
            validate_source_name("")

    def test_hyphens_rejected(self):
        with pytest.raises(InvalidSourceNameError, match="invalid"):
            validate_source_name("my-source")

    def test_spaces_rejected(self):
        with pytest.raises(InvalidSourceNameError, match="invalid"):
            validate_source_name("my source")

    def test_dots_rejected(self):
        with pytest.raises(InvalidSourceNameError, match="invalid"):
            validate_source_name("my.source")

    def test_path_traversal_rejected(self):
        with pytest.raises(InvalidSourceNameError, match="invalid"):
            validate_source_name("../etc/passwd")

    def test_slash_rejected(self):
        with pytest.raises(InvalidSourceNameError, match="invalid"):
            validate_source_name("source/name")

    def test_too_long(self):
        name = "a" * 129
        with pytest.raises(InvalidSourceNameError, match="at most 128"):
            validate_source_name(name)

    def test_max_length_ok(self):
        name = "a" * 128
        assert validate_source_name(name) == name


@pytest.mark.unit
class TestValidateDateString:
    """Tests for validate_date_string()."""

    def test_valid_date(self):
        result = validate_date_string("2024-01-15")
        assert isinstance(result, datetime)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 15

    def test_invalid_format(self):
        with pytest.raises(InvalidDateFormatError, match="Invalid date"):
            validate_date_string("15/01/2024")

    def test_invalid_date(self):
        with pytest.raises(InvalidDateFormatError, match="Invalid date"):
            validate_date_string("2024-13-45")

    def test_empty_string(self):
        with pytest.raises(InvalidDateFormatError, match="Invalid date"):
            validate_date_string("")

    def test_custom_format(self):
        result = validate_date_string("15/01/2024", fmt="%d/%m/%Y")
        assert result.day == 15
        assert result.month == 1

    def test_preserves_original_chain(self):
        with pytest.raises(InvalidDateFormatError) as exc_info:
            validate_date_string("not-a-date")
        assert exc_info.value.__cause__ is not None  # chained from ValueError


@pytest.mark.unit
class TestValidatePortRange:
    """Tests for validate_port_range()."""

    def test_valid_port(self):
        assert validate_port_range(8080) == 8080

    def test_min_port(self):
        assert validate_port_range(1) == 1

    def test_max_port(self):
        assert validate_port_range(65535) == 65535

    def test_zero_rejected(self):
        with pytest.raises(InvalidPortError, match="out of range"):
            validate_port_range(0)

    def test_negative_rejected(self):
        with pytest.raises(InvalidPortError, match="out of range"):
            validate_port_range(-1)

    def test_too_high(self):
        with pytest.raises(InvalidPortError, match="out of range"):
            validate_port_range(65536)

    def test_non_int_rejected(self):
        with pytest.raises(InvalidPortError, match="integer"):
            validate_port_range("8080")  # type: ignore[arg-type]

    def test_bool_rejected(self):
        with pytest.raises(InvalidPortError, match="integer"):
            validate_port_range(True)  # type: ignore[arg-type]


@pytest.mark.unit
class TestValidateLimit:
    """Tests for validate_limit()."""

    def test_normal_value(self):
        assert validate_limit(100) == 100

    def test_clamp_to_max(self):
        assert validate_limit(20000) == 10000

    def test_clamp_to_min(self):
        assert validate_limit(0) == 1

    def test_negative(self):
        assert validate_limit(-5) == 1

    def test_custom_max(self):
        assert validate_limit(500, max_val=200) == 200

    def test_non_int_returns_1(self):
        assert validate_limit("100") == 1  # type: ignore[arg-type]

    def test_bool_returns_1(self):
        assert validate_limit(True) == 1  # type: ignore[arg-type]


@pytest.mark.unit
class TestSanitizePathComponent:
    """Tests for sanitize_path_component()."""

    def test_clean_name(self):
        assert sanitize_path_component("my_source") == "my_source"

    def test_removes_slashes(self):
        assert sanitize_path_component("foo/bar") == "foobar"

    def test_removes_backslashes(self):
        assert sanitize_path_component("foo\\bar") == "foobar"

    def test_removes_dot_dot(self):
        assert sanitize_path_component("../etc") == "etc"

    def test_removes_null_bytes(self):
        assert sanitize_path_component("foo\x00bar") == "foobar"

    def test_multiple_traversals(self):
        assert sanitize_path_component("../../etc/passwd") == "etcpasswd"

    def test_empty_string(self):
        assert sanitize_path_component("") == ""
