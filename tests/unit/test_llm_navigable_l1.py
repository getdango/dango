"""tests/unit/test_llm_navigable_l1.py

Tests for the LLM-Navigable Layer 1 CLAUDE.md scaffold — generation at
`dango init` plus the source/model inventory update hooks used by
`dango source add` and `dango model add`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.init import ProjectInitializer
from dango.cli.model_wizard import _update_claude_md_models
from dango.cli.source_wizard import _update_claude_md_sources
from dango.config.models import (
    DangoConfig,
    DataSource,
    ProjectContext,
    SourcesConfig,
    SourceType,
)


def _make_config(with_source: bool = False) -> DangoConfig:
    """Build a minimal DangoConfig, optionally with one configured source."""
    sources = (
        SourcesConfig(
            sources=[
                DataSource(
                    name="my_csv",
                    type=SourceType.CSV,
                    description="Sales export",
                )
            ]
        )
        if with_source
        else SourcesConfig()
    )
    return DangoConfig(
        project=ProjectContext(
            name="Test Project",
            created_by="tester",
            purpose="Track daily sales",
        ),
        sources=sources,
    )


@pytest.mark.unit
class TestCreateClaudeMd:
    """Tests for ProjectInitializer._create_claude_md()."""

    def test_claude_md_created_on_init(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        claude_path = tmp_path / "CLAUDE.md"
        assert claude_path.exists()

    def test_claude_md_not_overwritten(self, tmp_path: Path) -> None:
        claude_path = tmp_path / "CLAUDE.md"
        claude_path.write_text("# User-edited content\n\nDo not touch.\n")

        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        assert claude_path.read_text() == "# User-edited content\n\nDo not touch.\n"

    def test_claude_md_contains_project_name(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "Test Project" in content

    def test_claude_md_contains_data_layers(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "staging.*" in content
        assert "intermediate.*" in content
        assert "marts.*" in content

    def test_claude_md_contains_source_inventory(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config(with_source=True))

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "my_csv" in content
        assert "Sales export" in content

    def test_claude_md_no_sources_placeholder(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config(with_source=False))

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "No sources configured yet" in content


@pytest.mark.unit
class TestUpdateClaudeMdSources:
    """Tests for source_wizard._update_claude_md_sources()."""

    def test_update_sources_appends_entry(self, tmp_path: Path) -> None:
        claude_path = tmp_path / "CLAUDE.md"
        claude_path.write_text(
            "# Project\n\n## Data sources\n\n"
            "*(No sources configured yet — run `dango source add`)*\n\n"
            "## dbt models\n\n*(Models appear here as you create them)*\n"
        )

        _update_claude_md_sources(tmp_path, "stripe", "stripe", "Payments data")

        content = claude_path.read_text()
        assert "**stripe** (`stripe`): Payments data" in content
        # dbt models section is preserved untouched
        assert "## dbt models" in content
        assert "*(Models appear here as you create them)*" in content

    def test_update_sources_no_file(self, tmp_path: Path) -> None:
        # No CLAUDE.md present — should return silently, no error raised
        _update_claude_md_sources(tmp_path, "stripe", "stripe", "Payments data")
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_update_sources_no_marker(self, tmp_path: Path) -> None:
        # CLAUDE.md exists but has been heavily edited — marker section gone
        claude_path = tmp_path / "CLAUDE.md"
        claude_path.write_text("# Project\n\nCompletely custom content.\n")

        _update_claude_md_sources(tmp_path, "stripe", "stripe", "Payments data")

        # File is left untouched — no error, no corruption
        assert claude_path.read_text() == "# Project\n\nCompletely custom content.\n"


@pytest.mark.unit
class TestUpdateClaudeMdModels:
    """Tests for model_wizard._update_claude_md_models()."""

    def test_update_models_appends_entry(self, tmp_path: Path) -> None:
        claude_path = tmp_path / "CLAUDE.md"
        claude_path.write_text(
            "# Project\n\n## Data sources\n\n*(No sources configured yet)*\n\n"
            "## dbt models\n\n*(Models appear here as you create them with `dango model add`)*\n\n"
            "## Key commands\n\nSome commands here.\n"
        )

        _update_claude_md_models(tmp_path, "int_revenue.sql", "intermediate", "Revenue rollup")

        content = claude_path.read_text()
        assert "**int_revenue.sql** (`intermediate`): Revenue rollup" in content
        # Key commands section is preserved untouched
        assert "## Key commands" in content
        assert "Some commands here." in content

    def test_update_models_no_file(self, tmp_path: Path) -> None:
        _update_claude_md_models(tmp_path, "int_revenue.sql", "intermediate", "Revenue rollup")
        assert not (tmp_path / "CLAUDE.md").exists()

    def test_update_models_no_marker(self, tmp_path: Path) -> None:
        claude_path = tmp_path / "CLAUDE.md"
        claude_path.write_text("# Project\n\nCompletely custom content.\n")

        _update_claude_md_models(tmp_path, "int_revenue.sql", "intermediate", "Revenue rollup")

        assert claude_path.read_text() == "# Project\n\nCompletely custom content.\n"
