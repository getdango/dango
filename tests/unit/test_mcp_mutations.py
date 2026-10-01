"""tests/unit/test_mcp_mutations.py

Tests for the MCP mutation tools (dango/cli/commands/mcp_mutations.py):
add_source, list_source_types. Operate tools: see test_mcp_operations.py. Schedule tools:
see test_mcp_schedules.py; model tools: see test_mcp_models.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.commands import mcp_mutations


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real Dango project on disk (one CSV source named 'test_source'),
    with mcp_mutations._get_project_root() pointed at it — mirrors the
    monkeypatch pattern already used for mcp_server._get_project_root in
    test_mcp_server.py.
    """
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_mutations, "_get_project_root", lambda: tmp_path)
    return tmp_path


@pytest.mark.unit
class TestAddSource:
    def test_add_source_unknown_type(self, project: Path) -> None:
        result = mcp_mutations.add_source("not_a_real_source_type", "my_source")
        assert "error" in result
        assert "Unknown source type" in result["error"]

    def test_add_source_duplicate_name(self, project: Path) -> None:
        result = mcp_mutations.add_source("csv", "test_source")
        assert result == {"error": "Source 'test_source' already exists in sources.yml"}

    def test_add_source_success_writes_sources_yml_and_returns_next_steps(
        self, project: Path
    ) -> None:
        """Positive control for the save_config-argument-order bug: save_config's real
        signature is save_config(config, project_root=None). Reversed arguments would
        raise or silently fail to persist — assert the new source actually round-trips
        through disk, not just that the in-memory return dict looks right."""
        result = mcp_mutations.add_source("stripe", "my_stripe", description="Prod Stripe")

        assert result["status"] == "created"
        assert result["source_name"] == "my_stripe"
        assert result["auth_type"] == "api_key"
        assert any(
            "STRIPE_API_KEY" in step or "secrets.toml" in step for step in result["next_steps"]
        )
        assert any("dango sync my_stripe" in step for step in result["next_steps"])

        from dango.config.helpers import load_config

        reloaded = load_config(project)
        added = reloaded.sources.get_source("my_stripe")
        assert added is not None
        assert added.type.value == "stripe"
        assert added.description == "Prod Stripe"


@pytest.mark.unit
class TestListSourceTypes:
    def test_list_source_types_includes_known_wizard_enabled_type(self) -> None:
        result = mcp_mutations.list_source_types()
        types = {r["type"] for r in result}
        assert "stripe" in types
        stripe_entry = next(r for r in result if r["type"] == "stripe")
        assert stripe_entry["auth_type"] == "api_key"
        assert stripe_entry["name"] == "Stripe"


@pytest.mark.unit
class TestGitWarning:
    """1.0.8-OPS-3: add_source surfaces git-state
    warnings via a `git_warning` key in their return dict — there's no human
    to prompt over stdio, so this is how the calling agent sees it. The
    `project` fixture's tmp_path is never a git repo, so these tests
    monkeypatch mcp_mutations._git_warnings() directly rather than needing a
    real git repo; the underlying detection logic (on main/master, dirty
    tree) is covered separately in test_git_info.py::TestCheckMutationGuardrails."""

    def test_add_source_includes_git_warning_when_present(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            mcp_mutations, "_git_warnings", lambda project_root: ["On branch 'main' — ..."]
        )
        result = mcp_mutations.add_source("csv", "new_csv_source")
        assert result["status"] == "created"
        assert result["git_warning"] == ["On branch 'main' — ..."]

    def test_add_source_omits_git_warning_when_clean(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mcp_mutations, "_git_warnings", lambda project_root: [])
        result = mcp_mutations.add_source("csv", "another_csv_source")
        assert result["status"] == "created"
        assert "git_warning" not in result

    def test_add_source_error_path_has_no_git_warning(self, project: Path) -> None:
        """Validation failures (nothing written) don't need a git_warning key at all —
        only successful mutations do."""
        result = mcp_mutations.add_source("csv", "test_source")  # duplicate name
        assert result == {"error": "Source 'test_source' already exists in sources.yml"}
        assert "git_warning" not in result

    def test_non_git_project_never_crashes_and_omits_key(self, project: Path) -> None:
        """project fixture's tmp_path is a plain directory, never git-initialized —
        confirms _git_warnings() (and therefore both tools) handles a non-git
        project gracefully with no exception and no git_warning key, using the real
        (unmocked) collect_git_info()/check_mutation_guardrails() call chain."""
        result = mcp_mutations.add_source("csv", "non_git_source")
        assert result["status"] == "created"
        assert "git_warning" not in result
