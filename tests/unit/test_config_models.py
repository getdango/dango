"""Tests for dango.config.models — Pydantic models and enums."""

import pytest
from pydantic import ValidationError

from dango.config.models import DataSource, ProjectContext, SourceType


class TestSourceType:
    def test_source_type_has_expected_members(self):
        """SourceType enum contains key source types used by Dango."""
        expected = {"csv", "google_sheets", "hubspot", "stripe", "shopify", "slack"}
        actual = {member.value for member in SourceType}
        assert expected.issubset(actual)


class TestProjectContext:
    def test_project_context_creation(self, sample_project_context):
        """ProjectContext can be created with required fields and has correct values."""
        ctx = sample_project_context
        assert ctx.name == "Test Analytics"
        assert ctx.created_by == "test@example.com"
        assert ctx.purpose == "Unit testing the Dango config system"
        assert ctx.created is not None


class TestDataSource:
    def test_data_source_rejects_hyphenated_names(self):
        """DataSource name validator rejects hyphens and raises ValueError."""
        with pytest.raises(ValidationError, match="invalid"):
            DataSource(name="my-source", type=SourceType.CSV)
