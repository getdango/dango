"""tests/unit/test_llm_navigable_l1.py

Tests for the LLM-Navigable Layer 1 scaffold — CLAUDE.md and AGENTS.md
generation at `dango init`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.init import ProjectInitializer
from dango.config.models import (
    DangoConfig,
    ProjectContext,
    SourcesConfig,
)


def _make_config() -> DangoConfig:
    """Build a minimal DangoConfig."""
    return DangoConfig(
        project=ProjectContext(
            name="Test Project",
            created_by="tester",
            purpose="Track daily sales",
        ),
        sources=SourcesConfig(),
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

    def test_claude_md_no_source_or_model_inventory(self, tmp_path: Path) -> None:
        """Redundant with live MCP tools (list_sources/get_catalog/list_models) — see
        BUGS-FOUND.md. These sections were removed, not just left empty."""
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        content = (tmp_path / "CLAUDE.md").read_text()
        assert "## Data sources" not in content
        assert "## dbt models" not in content
        assert "list_sources()" in content
        assert "get_catalog()" in content


@pytest.mark.unit
class TestCreateAgentsMd:
    """Tests for ProjectInitializer._create_claude_md()'s AGENTS.md side effect."""

    def test_agents_md_created_on_init(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        agents_path = tmp_path / "AGENTS.md"
        assert agents_path.exists()

    def test_agents_md_matches_claude_md(self, tmp_path: Path) -> None:
        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        claude_content = (tmp_path / "CLAUDE.md").read_text()
        agents_content = (tmp_path / "AGENTS.md").read_text()
        assert claude_content == agents_content

    def test_agents_md_not_overwritten(self, tmp_path: Path) -> None:
        agents_path = tmp_path / "AGENTS.md"
        agents_path.write_text("# User-edited content\n\nDo not touch.\n")

        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        assert agents_path.read_text() == "# User-edited content\n\nDo not touch.\n"

    def test_agents_md_created_independently_of_claude_md_existing(self, tmp_path: Path) -> None:
        """If CLAUDE.md already exists (so _create_claude_md returns early per its
        top-level guard) but AGENTS.md doesn't yet, AGENTS.md should still NOT be
        created — the early return on an existing CLAUDE.md exits the whole method
        before either file is touched. This documents that behavior explicitly."""
        claude_path = tmp_path / "CLAUDE.md"
        claude_path.write_text("# Pre-existing\n")

        initializer = ProjectInitializer(tmp_path)
        initializer._create_claude_md(_make_config())

        assert not (tmp_path / "AGENTS.md").exists()
