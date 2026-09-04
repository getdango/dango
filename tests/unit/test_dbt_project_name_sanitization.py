"""tests/unit/test_dbt_project_name_sanitization.py

Tests for ProjectInitializer._create_dbt_project()'s project-name sanitization.
A project directory name containing a dot (e.g. "review-1.0.8") previously produced
an invalid dbt project name that failed dbt's own validation regex (^[^\\d\\W]\\w*$)
at `dbt docs generate` time, well after most of `dango init` had already run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dango.cli.init import ProjectInitializer
from tests.factories.config_factories import make_dango_config, make_project_context

# Mirrors dbt's own project-name validation regex.
_DBT_NAME_RE = re.compile(r"^[^\d\W]\w*$")


def _generated_name(tmp_path: Path, project_display_name: str) -> str:
    (tmp_path / "dbt" / "macros").mkdir(parents=True)
    initializer = ProjectInitializer(tmp_path)
    config = make_dango_config(project=make_project_context(name=project_display_name))
    initializer._create_dbt_project(config)
    content = (tmp_path / "dbt" / "dbt_project.yml").read_text()
    match = re.search(r"^name: '([^']*)'", content, re.MULTILINE)
    assert match is not None, "dbt_project.yml missing a name: field"
    return match.group(1)


@pytest.mark.unit
class TestDbtProjectNameSanitization:
    def test_dot_in_name_is_sanitized(self, tmp_path: Path) -> None:
        """A dot (e.g. from a directory like "review-1.0.8") must not reach dbt_project.yml."""
        name = _generated_name(tmp_path, "review-1.0.8")
        assert _DBT_NAME_RE.match(name), f"{name!r} does not match dbt's project-name regex"

    def test_leading_digit_is_sanitized(self, tmp_path: Path) -> None:
        """dbt's regex forbids a leading digit even after word-char sanitization."""
        name = _generated_name(tmp_path, "2026 Analytics")
        assert _DBT_NAME_RE.match(name), f"{name!r} does not match dbt's project-name regex"

    def test_normal_name_unaffected(self, tmp_path: Path) -> None:
        """Existing behavior for a plain name is unchanged."""
        name = _generated_name(tmp_path, "Test Analytics")
        assert name == "test_analytics"
