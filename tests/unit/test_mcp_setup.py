"""tests/unit/test_mcp_setup.py

Tests for `dango mcp setup` / `dango mcp status` / `dango mcp remove`
(dango/cli/commands/mcp_setup.py): LLM client detection, Claude Code's
native `claude mcp add/get/remove` subprocess calls (1.0.8-OPS-4), and the
hand-written-file fallback still used by Cursor (project-scoped) and
Windsurf (global).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dango.cli.commands.mcp_server import mcp_group


def _patch_project_context(monkeypatch: pytest.MonkeyPatch, project_root: Path) -> None:
    """mcp_setup/mcp_status/mcp_remove all lazily import
    `require_project_context` from `dango.cli.utils` inside their function
    bodies (the codebase-wide lazy-import CLI convention) — patching it at
    its definition module, not at mcp_setup's namespace, is what actually
    takes effect at call time. Mirrors the pattern already used for e.g.
    `dango.cli.commands.analyze` in test_cli_analyze.py."""
    monkeypatch.setattr("dango.cli.utils.require_project_context", lambda ctx: project_root)


@pytest.mark.unit
class TestMcpSetup:
    """dango mcp setup — LLM client detection + native install / config writing
    (1.0.8-OPS-4: Claude Code now shells out to `claude mcp add --scope local`
    instead of hand-writing ~/.claude/settings.json; Cursor writes a
    project-scoped, git-committable .cursor/mcp.json; Windsurf keeps the
    global hand-written-file fallback with DANGO_PROJECT_ROOT injected)."""

    def test_mcp_setup_writes_claude_code_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claude Code: shells out to `claude mcp add --scope local`, with --env
        placed *after* the positional name (a real bug found live: `-e/--env`
        is variadic and greedily swallows the next token if placed before it)."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        project_root = tmp_path / "myproject"
        project_root.mkdir()
        _patch_project_context(monkeypatch, project_root)

        captured: dict = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        cmd = captured["cmd"]
        assert cmd[:6] == ["claude", "mcp", "add", "--scope", "local", "dango"]
        assert cmd[6] == "--env"
        assert cmd[7] == f"DANGO_PROJECT_ROOT={project_root}"
        assert cmd[8] == "--"
        assert cmd[-2:] == ["mcp", "run"]
        assert "Claude Code" in result.output

    def test_mcp_setup_is_idempotent_on_rerun(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for a real bug found live while reviewing this PR:
        `claude mcp add` has no --force/overwrite flag and fails with exit 1
        ("MCP server dango already exists in local config") if an entry with
        this name is already registered. Re-running `dango mcp setup` a
        second time -- or after switching venvs, which is exactly the
        scenario this redesign exists to fix -- must not report a spurious
        failure. `_setup_claude_code` fixes this by removing any existing
        entry (ignoring the result) before adding, so setup stays idempotent
        and can actually update a changed dango_cmd/DANGO_PROJECT_ROOT."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        project_root = tmp_path / "myproject"
        project_root.mkdir()
        _patch_project_context(monkeypatch, project_root)

        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[2] == "remove":
                # Simulates the real, live-confirmed behavior: exit 1 with
                # nothing to remove yet on a first-ever setup run.
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr='No MCP server named "dango" in local scope'
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        assert len(calls) == 2
        assert calls[0][:3] == ["claude", "mcp", "remove"]
        assert calls[1][:3] == ["claude", "mcp", "add"]
        # The failed (nothing-to-remove) first call must not produce a
        # warning or block the add from running.
        assert "⚠" not in result.output
        assert "Claude Code" in result.output

    def test_mcp_setup_claude_cli_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`claude` CLI missing from PATH (distinct from Claude Code not being
        installed at all) -> clear warning, command doesn't crash, other
        clients still get processed."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        def _raise(cmd, **kwargs):
            raise FileNotFoundError("claude")

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _raise)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        assert "claude" in result.output.lower()
        assert "PATH" in result.output
        assert "No LLM clients detected" in result.output

    def test_mcp_setup_claude_add_nonzero_exit_warns_without_crashing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="something broke")

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        assert "something broke" in result.output

    def test_mcp_setup_writes_cursor_config_with_workspace_folder_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cursor: no native CLI, but its project-scoped .cursor/mcp.json
        supports ${workspaceFolder} substitution (confirmed against Cursor's
        own docs) -- write a bare 'dango' command + ${workspaceFolder}, not an
        absolute machine-specific path, since this file is meant to be
        committed to git."""
        home_dir = tmp_path / "home"
        (home_dir / ".cursor").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home_dir)
        project_root = tmp_path / "myproject"
        project_root.mkdir()
        _patch_project_context(monkeypatch, project_root)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        cursor_config = project_root / ".cursor" / "mcp.json"
        assert cursor_config.exists()
        written = json.loads(cursor_config.read_text())
        entry = written["mcpServers"]["dango"]
        assert entry["command"] == "dango"
        assert entry["env"]["DANGO_PROJECT_ROOT"] == "${workspaceFolder}"
        assert "Cursor" in result.output

    def test_mcp_setup_writes_windsurf_config_with_project_root_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windsurf: still global-only (confirmed against Windsurf's own 2026
        docs/changelog -- no project scoping exists), but DANGO_PROJECT_ROOT
        is now injected so at least the one configured project is
        unambiguous. Existing unrelated keys must survive the write."""
        home_dir = tmp_path / "home"
        windsurf_dir = home_dir / ".codeium" / "windsurf"
        windsurf_dir.mkdir(parents=True)
        (windsurf_dir / "mcp_config.json").write_text(json.dumps({"unrelated": "keep-me"}))
        monkeypatch.setattr(Path, "home", lambda: home_dir)
        project_root = tmp_path / "myproject"
        project_root.mkdir()
        _patch_project_context(monkeypatch, project_root)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        written = json.loads((windsurf_dir / "mcp_config.json").read_text())
        assert written["unrelated"] == "keep-me"
        entry = written["mcpServers"]["dango"]
        assert entry["env"]["DANGO_PROJECT_ROOT"] == str(project_root)
        assert "one Dango project at a time" in result.output

    def test_mcp_setup_no_clients(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """No LLM client dirs exist -> helpful message, no error, no files written."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["setup"])

        assert result.exit_code == 0
        assert "No LLM clients detected" in result.output
        assert not (tmp_path / ".claude").exists()


@pytest.mark.unit
class TestMcpRemove:
    """dango mcp remove — reverses whatever `dango mcp setup` configured."""

    def test_reverses_claude_code_setup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        captured: dict = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["remove"])

        assert result.exit_code == 0
        assert captured["cmd"] == ["claude", "mcp", "remove", "dango", "--scope", "local"]
        assert "Claude Code" in result.output

    def test_removes_cursor_and_windsurf_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "home"
        monkeypatch.setattr(Path, "home", lambda: home_dir)
        project_root = tmp_path / "myproject"
        project_root.mkdir()
        _patch_project_context(monkeypatch, project_root)

        cursor_config = project_root / ".cursor" / "mcp.json"
        cursor_config.parent.mkdir(parents=True)
        cursor_config.write_text(json.dumps({"mcpServers": {"dango": {"command": "dango"}}}))

        windsurf_config = home_dir / ".codeium" / "windsurf" / "mcp_config.json"
        windsurf_config.parent.mkdir(parents=True)
        windsurf_config.write_text(json.dumps({"mcpServers": {"dango": {"command": "dango"}}}))

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["remove"])

        assert result.exit_code == 0
        assert "dango" not in json.loads(cursor_config.read_text())["mcpServers"]
        assert "dango" not in json.loads(windsurf_config.read_text())["mcpServers"]
        assert "Cursor" in result.output
        assert "Windsurf" in result.output

    def test_nothing_to_remove(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["remove"])

        assert result.exit_code == 0
        assert "Nothing to remove" in result.output

    def test_claude_code_nothing_to_remove_is_silent_not_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`claude mcp remove` on a name that was never configured exits 1
        with "No MCP server named ... in local scope" (confirmed live) --
        an expected no-op, not a failure. Must be silent, matching how
        Cursor/Windsurf's _remove_mcp_config() silently returns False for
        the same case, not printed as a scary yellow warning on every
        `dango mcp remove` run from a project that only uses another client."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr='No MCP server named "dango" in local scope'
            )

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["remove"])

        assert result.exit_code == 0
        assert "⚠" not in result.output
        assert "Claude Code" not in result.output
        assert "Nothing to remove" in result.output

    def test_claude_code_remove_genuine_failure_still_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real failure (not the expected "nothing to remove" case) must
        still surface a warning, not be silently swallowed."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="something broke")

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["remove"])

        assert result.exit_code == 0
        assert "something broke" in result.output


@pytest.mark.unit
class TestMcpStatus:
    """dango mcp status — verification output."""

    def test_mcp_status_no_clients(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["status"])

        assert result.exit_code == 0
        assert "No LLM clients detected" in result.output

    def test_mcp_status_claude_code_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Status now shells out to `claude mcp get dango` rather than parsing
        ~/.claude.json directly -- that file's internal format is Claude
        Code's own implementation detail, not a contract for us to parse."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="dango:\n  Scope: Local config (private to you in this project)\n",
                stderr="",
            )

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["status"])

        assert result.exit_code == 0
        assert "Claude Code: dango MCP configured" in result.output

    def test_mcp_status_claude_code_not_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        _patch_project_context(monkeypatch, tmp_path / "myproject")

        def _fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="No MCP server found with name: dango"
            )

        monkeypatch.setattr("dango.cli.commands.mcp_setup.subprocess.run", _fake_run)

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["status"])

        assert result.exit_code == 0
        assert "run `dango mcp setup`" in result.output

    def test_mcp_status_cursor_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home_dir = tmp_path / "home"
        (home_dir / ".cursor").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home_dir)
        project_root = tmp_path / "myproject"
        project_root.mkdir()
        _patch_project_context(monkeypatch, project_root)

        cursor_config = project_root / ".cursor" / "mcp.json"
        cursor_config.parent.mkdir(parents=True)
        cursor_config.write_text(json.dumps({"mcpServers": {"dango": {"command": "dango"}}}))

        runner = CliRunner()
        result = runner.invoke(mcp_group, ["status"])

        assert result.exit_code == 0
        assert "Cursor: dango MCP configured" in result.output
