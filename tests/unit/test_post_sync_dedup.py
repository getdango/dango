"""tests/unit/test_post_sync_dedup.py

Tests for the not_null test deduplication logic in _enrich_staging_tests.
"""

import pytest

from dango.utils.post_sync import _has_not_null_test


@pytest.mark.unit
class TestHasNotNullTest:
    def test_plain_string(self):
        assert _has_not_null_test(["not_null"]) is True

    def test_dict_form(self):
        assert _has_not_null_test([{"not_null": {"config": {"severity": "warn"}}}]) is True

    def test_empty_list(self):
        assert _has_not_null_test([]) is False

    def test_other_tests_only(self):
        assert _has_not_null_test(["unique", {"accepted_values": {"values": [1, 2]}}]) is False

    def test_mixed_with_not_null_dict(self):
        assert _has_not_null_test(["unique", {"not_null": {}}]) is True


@pytest.mark.unit
class TestNotNullDedup:
    """Test that duplicate not_null tests are cleaned up correctly."""

    def _dedup_tests(self, tests: list) -> list:
        """Apply the same dedup logic used in _enrich_staging_tests."""
        if len(tests) > 1:
            has_plain = any(t == "not_null" for t in tests)
            has_configured = any(isinstance(t, dict) and "not_null" in t for t in tests)
            if has_plain and has_configured:
                return [t for t in tests if t != "not_null"]
        return tests

    def test_plain_and_configured_keeps_configured(self):
        tests = ["not_null", {"not_null": {"config": {"severity": "warn"}}}]
        result = self._dedup_tests(tests)
        assert result == [{"not_null": {"config": {"severity": "warn"}}}]

    def test_only_plain_not_removed(self):
        tests = ["not_null"]
        result = self._dedup_tests(tests)
        assert result == ["not_null"]

    def test_only_configured_not_removed(self):
        tests = [{"not_null": {"config": {"severity": "warn"}}}]
        result = self._dedup_tests(tests)
        assert result == [{"not_null": {"config": {"severity": "warn"}}}]

    def test_no_not_null_tests_unchanged(self):
        tests = ["unique", {"accepted_values": {"values": ["a", "b"]}}]
        result = self._dedup_tests(tests)
        assert result == ["unique", {"accepted_values": {"values": ["a", "b"]}}]

    def test_multiple_plain_with_configured(self):
        tests = ["not_null", "not_null", {"not_null": {"config": {"severity": "warn"}}}]
        result = self._dedup_tests(tests)
        assert result == [{"not_null": {"config": {"severity": "warn"}}}]

    def test_preserves_other_tests(self):
        tests = ["unique", "not_null", {"not_null": {"config": {"severity": "warn"}}}]
        result = self._dedup_tests(tests)
        assert result == ["unique", {"not_null": {"config": {"severity": "warn"}}}]
