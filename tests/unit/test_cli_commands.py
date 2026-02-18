"""tests/unit/test_cli_commands.py

Smoke tests for CLI command module registration.

Verifies all command modules import correctly and all expected
commands appear in the CLI help output.
"""

import pytest
from click.testing import CliRunner

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
            source,  # noqa: F401
            transform,  # noqa: F401
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
            "docs",
            "generate",
            "info",
            "init",
            "metabase",
            "model",
            "oauth",
            "rename",
            "run",
            "source",
            "start",
            "status",
            "stop",
            "sync",
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

    def test_no_circular_imports(self) -> None:
        """CLI main module imports without circular dependency errors."""
        from dango.cli.main import cli as cli_group  # noqa: F811

        assert cli_group is not None
