"""tests/unit/test_mcp_setup_helpers.py

Tests for the plain-function helpers in dango/cli/commands/mcp_setup.py:
`_resolve_dango_cmd()` (venv console-script resolution) and the shared
atomic-write helpers `_write_mcp_config()`/`_remove_mcp_config()` used by
the Cursor (project-scoped) and Windsurf (global) config-file fallback.
See tests/unit/test_mcp_setup.py for the `mcp setup`/`status`/`remove`
CLI command tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dango.cli.commands import mcp_setup


@pytest.mark.unit
class TestResolveDangoCmd:
    """_resolve_dango_cmd() — venv console-script resolution, shared by the
    Claude Code and Windsurf branches of `dango mcp setup`."""

    def test_resolves_venv_console_script_next_to_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the sys.executable.replace() bug: on a
        `pythonX.Y`-named interpreter (this repo's own documented venv setup,
        `python3.11 -m venv venv`), a substring replace of '/bin/python' ->
        '/bin/dango' leaves a bogus '/bin/dango3.11' path. The console script
        must be found by looking next to the interpreter instead."""
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python3.11"
        fake_python.write_text("")
        fake_dango = venv_bin / "dango"
        fake_dango.write_text("")
        monkeypatch.setattr("sys.executable", str(fake_python))

        assert mcp_setup._resolve_dango_cmd() == str(fake_dango)

    def test_falls_back_to_bare_dango_when_no_sibling_script(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No dango console script next to the interpreter -> falls back to bare
        'dango' on PATH, rather than resolving a nonexistent path."""
        venv_bin = tmp_path / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        fake_python = venv_bin / "python3.11"
        fake_python.write_text("")
        monkeypatch.setattr("sys.executable", str(fake_python))

        assert mcp_setup._resolve_dango_cmd() == "dango"


@pytest.mark.unit
class TestWriteAndRemoveMcpConfig:
    """_write_mcp_config() / _remove_mcp_config() — the shared atomic-write
    helpers still used by the Cursor and Windsurf branches."""

    def test_write_preserves_file_permissions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the tempfile.mkstemp() permission-downgrade bug:
        mkstemp() always creates its temp file at mode 0600 regardless of the
        target's prior mode, so a naive tmp-file+os.replace atomic write would
        silently tighten a config file from 0644 to 0600 on every write."""
        import stat

        config_path = tmp_path / "mcp.json"
        config_path.write_text(json.dumps({"theme": "dark"}))
        config_path.chmod(0o644)

        mcp_setup._write_mcp_config(config_path, {"command": "dango", "args": ["mcp", "run"]})

        mode = stat.S_IMODE(config_path.stat().st_mode)
        assert mode == 0o644, f"expected config to stay 0644, got {oct(mode)}"

    def test_write_preserves_existing_unrelated_keys(self, tmp_path: Path) -> None:
        """Existing unrelated keys in the config file are not clobbered."""
        config_path = tmp_path / "mcp.json"
        config_path.write_text(json.dumps({"theme": "dark"}))

        mcp_setup._write_mcp_config(config_path, {"command": "dango", "args": ["mcp", "run"]})

        written = json.loads(config_path.read_text())
        assert written["theme"] == "dark"
        assert written["mcpServers"]["dango"]["args"] == ["mcp", "run"]

    def test_remove_deletes_dango_key_only(self, tmp_path: Path) -> None:
        config_path = tmp_path / "mcp.json"
        config_path.write_text(
            json.dumps(
                {
                    "theme": "dark",
                    "mcpServers": {
                        "dango": {"command": "dango", "args": ["mcp", "run"]},
                        "other": {"command": "other"},
                    },
                }
            )
        )

        removed = mcp_setup._remove_mcp_config(config_path)

        assert removed is True
        written = json.loads(config_path.read_text())
        assert written["theme"] == "dark"
        assert "dango" not in written["mcpServers"]
        assert "other" in written["mcpServers"]

    def test_remove_returns_false_when_nothing_to_remove(self, tmp_path: Path) -> None:
        config_path = tmp_path / "mcp.json"
        assert mcp_setup._remove_mcp_config(config_path) is False

        config_path.write_text(json.dumps({"mcpServers": {"other": {}}}))
        assert mcp_setup._remove_mcp_config(config_path) is False
