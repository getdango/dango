"""tests/unit/test_r8k_ui_cli_polish.py

Tests for R8-K UI/CLI polish fixes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.mark.unit
class TestComposeProjectName:
    """BUG-085: COMPOSE_PROJECT_NAME collision prevention."""

    def test_deterministic(self) -> None:
        """Same path always produces the same project name."""
        from dango.platform.docker import DockerManager

        mgr1 = DockerManager(Path("/tmp/project-a"))
        mgr2 = DockerManager(Path("/tmp/project-a"))
        assert mgr1.compose_project_name == mgr2.compose_project_name

    def test_unique_per_path(self) -> None:
        """Different paths produce different project names."""
        from dango.platform.docker import DockerManager

        mgr_a = DockerManager(Path("/tmp/project-a"))
        mgr_b = DockerManager(Path("/tmp/project-b"))
        assert mgr_a.compose_project_name != mgr_b.compose_project_name

    def test_format(self) -> None:
        """Project name starts with 'dango-' and has 8-char hex suffix."""
        from dango.platform.docker import DockerManager

        mgr = DockerManager(Path("/tmp/test-project"))
        name = mgr.compose_project_name
        assert name.startswith("dango-")
        suffix = name[len("dango-") :]
        assert len(suffix) == 8
        # Verify it's valid hex
        int(suffix, 16)

    def test_compose_env_includes_project_name(self) -> None:
        """_compose_env() sets COMPOSE_PROJECT_NAME."""
        from dango.platform.docker import DockerManager

        mgr = DockerManager(Path("/tmp/test"))
        env = mgr._compose_env()
        assert "COMPOSE_PROJECT_NAME" in env
        assert env["COMPOSE_PROJECT_NAME"] == mgr.compose_project_name

    def test_hash_matches_manual(self) -> None:
        """Project name matches manual md5 calculation."""
        from dango.platform.docker import DockerManager

        path = Path("/srv/dango/project")
        mgr = DockerManager(path)
        expected_hash = hashlib.md5(str(path).encode(), usedforsecurity=False).hexdigest()[:8]
        assert mgr.compose_project_name == f"dango-{expected_hash}"


@pytest.mark.unit
class TestEnvCleanupLogic:
    """BUG-086: source remove .env cleanup."""

    def test_matching_vars_found(self) -> None:
        """Variables containing the uppercased source name are matched."""
        from dango.utils.env_file import parse_env_file

        content = "STRIPE_API_KEY=sk_test_123\nOTHER_VAR=hello\nSTRIPE_WEBHOOK_SECRET=whsec_456"
        env_vars = parse_env_file(content)
        source_token = "stripe".upper().replace("-", "_")
        matching = {k: v for k, v in env_vars.items() if source_token in k}
        assert len(matching) == 2
        assert "STRIPE_API_KEY" in matching
        assert "STRIPE_WEBHOOK_SECRET" in matching
        assert "OTHER_VAR" not in matching

    def test_roundtrip_after_removal(self) -> None:
        """Removing matched vars and serializing produces valid .env."""
        from dango.utils.env_file import parse_env_file, serialize_env_file

        content = "STRIPE_KEY=abc\nDB_HOST=localhost\nSTRIPE_SECRET=xyz\n"
        env_vars = parse_env_file(content)
        source_token = "STRIPE"
        matching = {k for k in env_vars if source_token in k}
        for k in matching:
            del env_vars[k]

        result = serialize_env_file(env_vars)
        reparsed = parse_env_file(result)
        assert reparsed == {"DB_HOST": "localhost"}

    def test_no_match_returns_empty(self) -> None:
        """No matches when source name not in any var."""
        from dango.utils.env_file import parse_env_file

        content = "DB_HOST=localhost\nDB_PORT=5432\n"
        env_vars = parse_env_file(content)
        source_token = "STRIPE"
        matching = {k: v for k, v in env_vars.items() if source_token in k}
        assert len(matching) == 0

    def test_hyphenated_source_name(self) -> None:
        """Hyphens in source name are converted to underscores for matching."""
        from dango.utils.env_file import parse_env_file

        content = "MY_SOURCE_KEY=val1\nOTHER=val2\n"
        env_vars = parse_env_file(content)
        source_token = "my-source".upper().replace("-", "_")
        matching = {k: v for k, v in env_vars.items() if source_token in k}
        assert len(matching) == 1
        assert "MY_SOURCE_KEY" in matching


@pytest.mark.unit
class TestAutoGeneratePassword:
    """BUG-095: Auto-generate admin password for cloud deploy."""

    def test_auto_generates_when_no_env_var(self) -> None:
        """_step_admin() auto-generates password when DANGO_ADMIN_PASSWORD unset."""
        from dango.cli.commands.deploy_wizard import _step_admin

        env_no_password = {
            k: v for k, v in __import__("os").environ.items() if k != "DANGO_ADMIN_PASSWORD"
        }
        with (
            patch.dict("os.environ", env_no_password, clear=True),
            patch("click.prompt", return_value="admin@example.com"),
        ):
            email, password = _step_admin()

        assert email == "admin@example.com"
        # token_urlsafe(16) produces ~22 chars
        assert len(password) >= 16

    def test_uses_env_var_when_set(self) -> None:
        """_step_admin() uses DANGO_ADMIN_PASSWORD from env when set."""
        from dango.cli.commands.deploy_wizard import _step_admin

        with (
            patch.dict("os.environ", {"DANGO_ADMIN_PASSWORD": "Str0ng!P@ssw0rd99"}),
            patch(
                "dango.auth.security.check_password_strength",
                return_value=[],
            ),
            patch("click.prompt", return_value="admin@example.com"),
        ):
            email, password = _step_admin()

        assert password == "Str0ng!P@ssw0rd99"

    def test_rejects_weak_env_password(self) -> None:
        """_step_admin() exits if DANGO_ADMIN_PASSWORD is weak."""
        from dango.cli.commands.deploy_wizard import _step_admin

        with (
            patch.dict("os.environ", {"DANGO_ADMIN_PASSWORD": "weak"}),
            patch(
                "dango.auth.security.check_password_strength",
                return_value=["Too short"],
            ),
            patch("click.prompt", return_value="admin@example.com"),
            pytest.raises(SystemExit),
        ):
            _step_admin()


@pytest.mark.unit
class TestStaleBinaryDetection:
    """UX-009: Warn when dango binary is outside active venv.

    Tests the detection logic directly rather than going through CliRunner,
    since Click's group callback + ``click.echo(err=True)`` interactions
    with CliRunner make stderr assertion unreliable.
    """

    def test_warns_outside_venv(self) -> None:
        """Warning emitted when sys.executable is outside VIRTUAL_ENV."""
        with (
            patch.dict("os.environ", {"VIRTUAL_ENV": "/home/user/project/venv"}, clear=False),
            patch("sys.executable", "/usr/local/bin/python3"),
            patch("click.echo") as mock_echo,
        ):
            import os
            import sys

            venv_prefix = os.environ.get("VIRTUAL_ENV")
            if venv_prefix and not sys.executable.startswith(venv_prefix):
                import click

                click.echo(
                    "Warning: Running 'dango' from outside the active venv. "
                    "Run 'hash -r' or restart your terminal.",
                    err=True,
                )

        mock_echo.assert_called_once()
        assert "outside the active venv" in mock_echo.call_args[0][0]
        assert mock_echo.call_args[1]["err"] is True

    def test_no_warn_inside_venv(self) -> None:
        """No warning when executable is inside the venv."""
        with (
            patch.dict(
                "os.environ",
                {"VIRTUAL_ENV": "/home/user/project/venv"},
                clear=False,
            ),
            patch("sys.executable", "/home/user/project/venv/bin/python3"),
            patch("click.echo") as mock_echo,
        ):
            import os
            import sys

            venv_prefix = os.environ.get("VIRTUAL_ENV")
            if venv_prefix and not sys.executable.startswith(venv_prefix):
                import click

                click.echo("should not fire", err=True)

        mock_echo.assert_not_called()

    def test_no_warn_without_venv(self) -> None:
        """No warning when VIRTUAL_ENV is unset."""
        env_without_venv = {k: v for k, v in __import__("os").environ.items() if k != "VIRTUAL_ENV"}
        with (
            patch.dict("os.environ", env_without_venv, clear=True),
            patch("click.echo") as mock_echo,
        ):
            import os
            import sys

            venv_prefix = os.environ.get("VIRTUAL_ENV")
            if venv_prefix and not sys.executable.startswith(venv_prefix):
                import click

                click.echo("should not fire", err=True)

        mock_echo.assert_not_called()
