"""tests/unit/test_cli_commands.py

Smoke tests for CLI command module registration.

Verifies all command modules import correctly and all expected
commands appear in the CLI help output.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.model import model_add
from dango.cli.commands.source import source_add
from dango.cli.main import cli


@pytest.mark.unit
class TestCliCommandRegistration:
    """Verify all CLI commands are properly registered."""

    def test_import_all_command_modules(self) -> None:
        """All command modules import without errors."""
        from dango.cli.commands import (
            auth,  # noqa: F401
            config_cmd,  # noqa: F401
            dashboard,  # noqa: F401
            data,  # noqa: F401
            metabase_cmd,  # noqa: F401
            model,  # noqa: F401
            oauth,  # noqa: F401
            platform,  # noqa: F401
            project,  # noqa: F401
            schedule,  # noqa: F401
            schedule_webhook,  # noqa: F401
            seed,  # noqa: F401
            source,  # noqa: F401
            transform,  # noqa: F401
            upgrade,  # noqa: F401
            web,  # noqa: F401
        )

    def test_cli_help_succeeds(self) -> None:
        """``dango --help`` exits cleanly."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0

    def test_all_toplevel_commands_registered(self) -> None:
        """Every expected top-level command appears in ``--help``."""
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        expected_commands = [
            "auth",
            "config",
            "dashboard",
            "db",
            "deploy",
            "docs",
            "generate",
            "info",
            "init",
            "metabase",
            "migrate",
            "model",
            "oauth",
            "remote",
            "rename",
            "run",
            "schedule",
            "seed",
            "serve",
            "source",
            "start",
            "status",
            "stop",
            "sync",
            "upgrade",
            "validate",
            "web",
        ]
        for cmd in expected_commands:
            assert cmd in result.output, f"Command '{cmd}' missing from --help output"

    def test_source_subcommands(self) -> None:
        """Source group has add, list, remove subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["source", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output

    def test_oauth_subcommands(self) -> None:
        """OAuth group has all expected subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["oauth", "--help"])
        assert result.exit_code == 0
        for cmd in [
            "check",
            "facebook_ads",
            "google_ads",
            "google_analytics",
            "google_sheets",
            "list",
            "refresh",
            "remove",
            "setup",
            "status",
        ]:
            assert cmd in result.output, f"OAuth subcommand '{cmd}' missing"

    def test_auth_subcommands(self) -> None:
        """Auth group has all expected subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "--help"])
        assert result.exit_code == 0
        for cmd in [
            "enable",
            "disable",
            "add-user",
            "list-users",
            "reset-password",
            "deactivate-user",
            "reactivate-user",
            "delete-user",
            "status",
            "unlock",
            "audit",
            "recover",
        ]:
            assert cmd in result.output, f"Auth subcommand '{cmd}' missing"

    def test_db_subcommands(self) -> None:
        """DB group has status and clean subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["db", "--help"])
        assert result.exit_code == 0
        assert "status" in result.output
        assert "clean" in result.output

    def test_config_subcommands(self) -> None:
        """Config group has validate and show subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output
        assert "show" in result.output

    def test_model_subcommands(self) -> None:
        """Model group has add and remove subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["model", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "remove" in result.output

    def test_seed_subcommands(self) -> None:
        """Seed group has add and list subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["seed", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output

    def test_metabase_subcommands(self) -> None:
        """Metabase group has save, load, refresh subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["metabase", "--help"])
        assert result.exit_code == 0
        assert "save" in result.output
        assert "load" in result.output
        assert "refresh" in result.output

    def test_dashboard_subcommands(self) -> None:
        """Dashboard group has provision subcommand."""
        runner = CliRunner()
        result = runner.invoke(cli, ["dashboard", "--help"])
        assert result.exit_code == 0
        assert "provision" in result.output

    def test_schedule_subcommands(self) -> None:
        """Schedule group has all expected subcommands."""
        runner = CliRunner()
        result = runner.invoke(cli, ["schedule", "--help"])
        assert result.exit_code == 0
        for cmd in ["list", "status", "add", "remove", "enable", "disable", "webhook"]:
            assert cmd in result.output, f"Schedule subcommand '{cmd}' missing"

    def test_no_circular_imports(self) -> None:
        """CLI main module imports without circular dependency errors."""
        from dango.cli.main import cli as cli_group  # noqa: F811

        assert cli_group is not None


def _init_git_repo(path: Path, branch: str = "main") -> None:
    """Create a real git repo at `path` on the given branch (main is git's
    default init branch on modern git, so no checkout needed for that case)."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "checkout", "-b", branch], check=True, capture_output=True
    )
    (path / "committed.txt").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True
    )


@pytest.mark.unit
class TestGitWarningDeduplication:
    """1.0.8-Q1: `model add` and `source add` used to print both the old
    boxed 'Git Branch Reminder' panel (check_git_branch_warning(),
    cli/utils.py) and the newer plain-text warning from the wizard's own
    _print_git_warnings() (model_wizard.py / source_wizard.py). The boxed
    panel call was removed from both CLI commands — only the wizard's own
    plain warning should remain.
    """

    def test_model_add_on_main_shows_only_one_warning(self, tmp_path: Path) -> None:
        """`dango model add` on a main-branch repo prints the wizard's plain
        'Warning:' line exactly once, and never the old boxed panel.

        No dbt/ directory exists, so ModelWizard.run() prints its intro,
        prints the git warning, then exits early with 'dbt directory not
        found' — this exercises the real warning path without needing to
        mock the interactive question prompts.
        """
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "project.yml").write_text(
            "project:\n  name: test\n  created_by: test\n  purpose: test project\n"
        )
        _init_git_repo(tmp_path, branch="main")

        runner = CliRunner()
        with patch("dango.cli.utils.require_project_context", return_value=tmp_path):
            result = runner.invoke(model_add, obj={"project_root": tmp_path})

        assert "Git Branch Reminder" not in result.output
        assert result.output.count("Warning:") == 1

    def test_source_add_on_main_shows_only_one_warning(self, tmp_path: Path) -> None:
        """`dango source add` on a main-branch repo prints the wizard's plain
        'Warning:' line exactly once, and never the old boxed panel.

        Only the interactive source-type selection is mocked (to cancel
        immediately) — everything before it, including the git warning, is
        the real SourceWizard.run() path.
        """
        _init_git_repo(tmp_path, branch="main")

        runner = CliRunner()
        with patch("dango.cli.source_wizard.SourceWizard._select_source_flat", return_value=None):
            result = runner.invoke(source_add, obj={"project_root": tmp_path})

        assert "Git Branch Reminder" not in result.output
        assert result.output.count("Warning:") == 1
