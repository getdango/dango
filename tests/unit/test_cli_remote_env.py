"""tests/unit/test_cli_remote_env.py

Tests for dango.cli.commands.remote_env — remote environment variable management.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote_env import (
    _parse_env_file,
    _serialize_env_file,
    env,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from Rich console output."""
    return _ANSI_RE.sub("", text)


@pytest.mark.unit
class TestParseEnvFile:
    """Tests for _parse_env_file."""

    def test_simple_key_value(self):
        content = "FOO=bar\nBAZ=qux\n"
        result = _parse_env_file(content)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_double_quoted_value(self):
        content = 'KEY="hello world"\n'
        result = _parse_env_file(content)
        assert result == {"KEY": "hello world"}

    def test_single_quoted_value(self):
        content = "KEY='hello world'\n"
        result = _parse_env_file(content)
        assert result == {"KEY": "hello world"}

    def test_skips_comments(self):
        content = "# This is a comment\nFOO=bar\n# Another comment\n"
        result = _parse_env_file(content)
        assert result == {"FOO": "bar"}

    def test_skips_blank_lines(self):
        content = "FOO=bar\n\n\nBAZ=qux\n"
        result = _parse_env_file(content)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_skips_lines_without_equals(self):
        content = "FOO=bar\ninvalid-line\nBAZ=qux\n"
        result = _parse_env_file(content)
        assert result == {"FOO": "bar", "BAZ": "qux"}

    def test_empty_value(self):
        content = "KEY=\n"
        result = _parse_env_file(content)
        assert result == {"KEY": ""}

    def test_value_with_equals(self):
        content = "KEY=value=with=equals\n"
        result = _parse_env_file(content)
        assert result == {"KEY": "value=with=equals"}

    def test_empty_content(self):
        result = _parse_env_file("")
        assert result == {}

    def test_strips_whitespace(self):
        content = "  FOO  =  bar  \n"
        result = _parse_env_file(content)
        assert result == {"FOO": "bar"}


@pytest.mark.unit
class TestSerializeEnvFile:
    """Tests for _serialize_env_file."""

    def test_simple_values(self):
        env_vars = {"FOO": "bar", "BAZ": "qux"}
        result = _serialize_env_file(env_vars)
        assert "FOO=bar" in result
        assert "BAZ=qux" in result

    def test_value_with_spaces_is_quoted(self):
        env_vars = {"KEY": "hello world"}
        result = _serialize_env_file(env_vars)
        assert 'KEY="hello world"' in result

    def test_empty_dict(self):
        result = _serialize_env_file({})
        assert result == ""

    def test_roundtrip(self):
        original = {"FOO": "bar", "BAZ": "qux", "COMPLEX": "has spaces"}
        serialized = _serialize_env_file(original)
        parsed = _parse_env_file(serialized)
        assert parsed == original


# ---------------------------------------------------------------------------
# Shared mock helpers
# ---------------------------------------------------------------------------


def _make_ssh_mock(env_content: str = "") -> MagicMock:
    """Create a mock SSHManager that returns *env_content* from read_remote_file."""
    ssh = MagicMock()
    ssh.read_remote_file.return_value = env_content
    ssh.disconnect.return_value = None
    return ssh


def _patch_ssh_connect(ssh_mock: MagicMock, cloud_cfg: MagicMock | None = None):
    """Return a patch context for _ssh_connect_or_fail."""
    if cloud_cfg is None:
        cloud_cfg = MagicMock()
    from pathlib import Path

    return patch(
        "dango.cli.commands.remote._ssh_connect_or_fail",
        return_value=(cloud_cfg, ssh_mock, Path("/test/project")),
    )


@pytest.mark.unit
class TestEnvSet:
    """Tests for ``dango remote env set``."""

    def test_set_new_variable(self):
        ssh = _make_ssh_mock("")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["set", "FOO=bar"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Set" in _strip_ansi(result.output)
        assert "FOO=***" in _strip_ansi(result.output)
        ssh.write_remote_file.assert_called_once()
        written_content = ssh.write_remote_file.call_args[0][1]
        assert "FOO=bar" in written_content

    def test_update_existing_variable(self):
        ssh = _make_ssh_mock("FOO=old\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["set", "FOO=new"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Updated" in _strip_ansi(result.output)
        written_content = ssh.write_remote_file.call_args[0][1]
        assert "FOO=new" in written_content

    def test_invalid_format_no_equals(self):
        runner = CliRunner()
        ssh = _make_ssh_mock()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["set", "INVALID"])
        assert result.exit_code != 0
        assert "KEY=VALUE" in _strip_ansi(result.output)

    def test_empty_key_rejected(self):
        runner = CliRunner()
        ssh = _make_ssh_mock()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["set", "=value"])
        assert result.exit_code != 0
        assert "Key cannot be empty" in _strip_ansi(result.output)

    def test_disconnects_on_success(self):
        ssh = _make_ssh_mock("")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            runner.invoke(env, ["set", "K=V"], catch_exceptions=False)
        ssh.disconnect.assert_called_once()


@pytest.mark.unit
class TestEnvGet:
    """Tests for ``dango remote env get``."""

    def test_get_existing_variable(self):
        ssh = _make_ssh_mock("FOO=bar\nBAZ=qux\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["get", "FOO"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "FOO=***" in _strip_ansi(result.output)

    def test_get_missing_variable(self):
        ssh = _make_ssh_mock("FOO=bar\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["get", "MISSING"])
        assert result.exit_code != 0
        assert "Not found" in _strip_ansi(result.output)

    def test_disconnects_on_get(self):
        ssh = _make_ssh_mock("FOO=bar\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            runner.invoke(env, ["get", "FOO"], catch_exceptions=False)
        ssh.disconnect.assert_called_once()


@pytest.mark.unit
class TestEnvList:
    """Tests for ``dango remote env list``."""

    def test_list_variables(self):
        ssh = _make_ssh_mock("FOO=bar\nBAZ=secret\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["list"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "FOO=***" in _strip_ansi(result.output)
        assert "BAZ=***" in _strip_ansi(result.output)
        assert "2 variable(s)" in _strip_ansi(result.output)
        # Ensure raw values are never shown
        assert "bar" not in _strip_ansi(result.output)
        assert "secret" not in _strip_ansi(result.output)

    def test_list_empty(self):
        ssh = _make_ssh_mock("")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["list"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No environment variables" in _strip_ansi(result.output)

    def test_disconnects_on_list(self):
        ssh = _make_ssh_mock("")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            runner.invoke(env, ["list"], catch_exceptions=False)
        ssh.disconnect.assert_called_once()


@pytest.mark.unit
class TestEnvDelete:
    """Tests for ``dango remote env delete``."""

    def test_delete_existing_variable(self):
        ssh = _make_ssh_mock("FOO=bar\nBAZ=qux\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["delete", "FOO"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Deleted" in _strip_ansi(result.output)
        assert "FOO" in _strip_ansi(result.output)
        written_content = ssh.write_remote_file.call_args[0][1]
        assert "FOO" not in written_content
        assert "BAZ=qux" in written_content

    def test_delete_missing_variable(self):
        ssh = _make_ssh_mock("FOO=bar\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            result = runner.invoke(env, ["delete", "MISSING"])
        assert result.exit_code != 0
        assert "Not found" in _strip_ansi(result.output)

    def test_disconnects_on_delete(self):
        ssh = _make_ssh_mock("FOO=bar\n")
        runner = CliRunner()
        with _patch_ssh_connect(ssh):
            runner.invoke(env, ["delete", "FOO"], catch_exceptions=False)
        ssh.disconnect.assert_called_once()
