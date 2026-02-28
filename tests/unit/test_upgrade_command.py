"""tests/unit/test_upgrade_command.py

Unit tests for ``dango upgrade`` CLI command and version cache helper.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _mock_subprocess_ok(mock_subprocess: MagicMock) -> None:
    """Configure a mock subprocess module for successful pip calls."""
    mock_subprocess.run.return_value = MagicMock(returncode=0, stderr="")
    mock_subprocess.CalledProcessError = subprocess.CalledProcessError
    mock_subprocess.TimeoutExpired = subprocess.TimeoutExpired


def _setup_project(tmp_path: Path) -> Path:
    """Create minimal project structure for tests."""
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    project_yml = dango_dir / "project.yml"
    project_yml.write_text(
        "project:\n  name: test\n  version: '1.0'\n  created_by: tester\n  purpose: unit test\n"
    )
    return tmp_path


@pytest.mark.unit
class TestUpgradeHelp:
    """``dango upgrade --help`` works and command is registered."""

    def test_upgrade_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--help"])
        assert result.exit_code == 0
        assert "--version" in result.output
        assert "--yes" in result.output

    def test_import_upgrade_module(self) -> None:
        """Module imports without errors."""
        from dango.cli.commands import upgrade  # noqa: F401


# ---------------------------------------------------------------------------
# Version validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVersionValidation:
    """Tests for the ``_validate_version`` helper."""

    def test_valid_version(self) -> None:
        from dango.cli.commands.upgrade import _validate_version

        # Should not raise
        _validate_version("1.2.3")
        _validate_version("0.0.1")
        _validate_version("10.20.30")

    def test_invalid_version_raises(self) -> None:
        import click

        from dango.cli.commands.upgrade import _validate_version

        with pytest.raises(click.BadParameter):
            _validate_version("bad")

        with pytest.raises(click.BadParameter):
            _validate_version("1.2")

        with pytest.raises(click.BadParameter):
            _validate_version("v1.2.3")


# ---------------------------------------------------------------------------
# Version cache helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestVersionCache:
    """Tests for ``get_latest_version_cached``."""

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_uses_fresh_cache(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """Fresh cache (<24h) should be used without calling PyPI."""
        project_root = _setup_project(tmp_path)
        cache_path = project_root / ".dango" / "state" / "version_check.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "version": "2.0.0",
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result == "2.0.0"
        mock_pypi.assert_not_called()

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_refreshes_stale_cache(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """Stale cache (>24h) should trigger a fresh PyPI check."""
        project_root = _setup_project(tmp_path)
        cache_path = project_root / ".dango" / "state" / "version_check.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        stale_time = datetime.now(timezone.utc) - timedelta(hours=25)
        cache_path.write_text(
            json.dumps({"version": "1.0.0", "checked_at": stale_time.isoformat()})
        )

        mock_pypi.return_value = "2.0.0"

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result == "2.0.0"
        mock_pypi.assert_called_once()

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_no_cache_calls_pypi(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """Without cache, should call PyPI and create cache."""
        project_root = _setup_project(tmp_path)
        mock_pypi.return_value = "3.0.0"

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result == "3.0.0"
        mock_pypi.assert_called_once()

        # Verify cache was written
        cache_path = project_root / ".dango" / "state" / "version_check.json"
        assert cache_path.exists()
        data: dict[str, Any] = json.loads(cache_path.read_text())
        assert data["version"] == "3.0.0"
        assert "checked_at" in data

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_pypi_failure_returns_none(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """PyPI failure should return None, not raise."""
        project_root = _setup_project(tmp_path)
        mock_pypi.return_value = None

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result is None

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_pypi_failure_writes_negative_cache(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """PyPI failure should write a negative cache entry."""
        project_root = _setup_project(tmp_path)
        mock_pypi.return_value = None

        from dango.cli.commands.upgrade import get_latest_version_cached

        get_latest_version_cached(project_root)

        cache_path = project_root / ".dango" / "state" / "version_check.json"
        assert cache_path.exists()
        data: dict[str, Any] = json.loads(cache_path.read_text())
        assert data["version"] is None
        assert "checked_at" in data

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_negative_cache_expires_after_5min(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """Negative cache should expire after 5 minutes, not 24 hours."""
        project_root = _setup_project(tmp_path)
        cache_path = project_root / ".dango" / "state" / "version_check.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Write a 6-minute-old negative cache entry
        old_time = datetime.now(timezone.utc) - timedelta(minutes=6)
        cache_path.write_text(json.dumps({"version": None, "checked_at": old_time.isoformat()}))

        mock_pypi.return_value = "1.0.0"

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result == "1.0.0"
        mock_pypi.assert_called_once()

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_fresh_negative_cache_avoids_pypi(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """Fresh negative cache (<5min) should not call PyPI."""
        project_root = _setup_project(tmp_path)
        cache_path = project_root / ".dango" / "state" / "version_check.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        # Write a 1-minute-old negative cache entry
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=1)
        cache_path.write_text(json.dumps({"version": None, "checked_at": recent_time.isoformat()}))

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result is None
        mock_pypi.assert_not_called()

    @patch("dango.platform.cloud.server_status.check_latest_pypi_version")
    def test_corrupt_cache_falls_through(self, mock_pypi: MagicMock, tmp_path: Path) -> None:
        """Corrupt cache should fall through to PyPI check."""
        project_root = _setup_project(tmp_path)
        cache_path = project_root / ".dango" / "state" / "version_check.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text("not valid json")

        mock_pypi.return_value = "1.5.0"

        from dango.cli.commands.upgrade import get_latest_version_cached

        result = get_latest_version_cached(project_root)
        assert result == "1.5.0"
        mock_pypi.assert_called_once()


# ---------------------------------------------------------------------------
# Upgrade command
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpgradeCommand:
    """Tests for the ``dango upgrade`` command."""

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.get_latest_version_cached")
    @patch("dango.__version__", "1.0.0")
    def test_already_at_latest(
        self,
        mock_cache: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Exit cleanly when already at the latest version."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        mock_cache.return_value = "1.0.0"

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade"])
        plain = _strip_ansi(result.output)
        assert "no upgrade needed" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.get_latest_version_cached")
    @patch("dango.__version__", "0.1.0")
    def test_pypi_unavailable_no_version_flag(
        self,
        mock_cache: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Error when PyPI is unreachable and no --version specified."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        mock_cache.return_value = None

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade"])
        plain = _strip_ansi(result.output)
        assert "Could not determine latest version" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.__version__", "0.1.0")
    def test_invalid_version_rejected(
        self,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Invalid --version value is rejected."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--version", "bad"])
        plain = _strip_ansi(result.output)
        assert "Invalid version" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending")
    @patch("dango.__version__", "0.1.0")
    def test_yes_flag_skips_prompts(
        self,
        mock_migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """--yes skips all confirmation prompts."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        mock_migrations.return_value = {}

        _mock_subprocess_ok(mock_subprocess)

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])
        plain = _strip_ansi(result.output)
        assert "Upgrade complete" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.__version__", "0.1.0")
    def test_pip_failure_shows_error(
        self,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """pip install failure shows error message."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root

        _mock_subprocess_ok(mock_subprocess)
        mock_subprocess.run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1, stderr="ERROR: No matching distribution"),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--version", "99.0.0", "--yes"])
        plain = _strip_ansi(result.output)
        assert "pip install failed" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.__version__", "0.1.0")
    def test_pip_timeout_shows_error(
        self,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """pip install timeout shows clean error message."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root

        _mock_subprocess_ok(mock_subprocess)
        mock_subprocess.run.side_effect = [
            MagicMock(returncode=0),
            subprocess.TimeoutExpired(cmd="pip", timeout=300),
        ]

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])
        plain = _strip_ansi(result.output)
        assert "timed out" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending")
    @patch("dango.__version__", "0.1.0")
    def test_migration_failure_shows_guidance(
        self,
        mock_migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Migration failure shows recovery guidance."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        mock_migrations.side_effect = RuntimeError("migration broke")

        _mock_subprocess_ok(mock_subprocess)

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])
        plain = _strip_ansi(result.output)
        assert "Migration error" in plain
        assert "dango migrate run" in plain

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending")
    @patch("dango.__version__", "0.1.0")
    def test_specific_version_flag(
        self,
        mock_migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """--version X.Y.Z installs that specific version."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        mock_migrations.return_value = {}

        _mock_subprocess_ok(mock_subprocess)

        runner = CliRunner()
        runner.invoke(cli, ["upgrade", "--version", "2.0.0", "--yes"])

        # Verify pip was called with the specific version
        calls = mock_subprocess.run.call_args_list
        pip_install_call = calls[1]  # second call is pip install
        cmd_args = pip_install_call[0][0]
        assert "getdango==2.0.0" in cmd_args

    @patch("dango.cli.utils.find_project_root")
    @patch("dango.cli.commands.upgrade.subprocess")
    @patch("dango.migrations.apply_all_pending")
    @patch("dango.__version__", "2.0.0")
    def test_downgrade_shows_note(
        self,
        mock_migrations: MagicMock,
        mock_subprocess: MagicMock,
        mock_root: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Downgrade shows a note about direction."""
        project_root = _setup_project(tmp_path)
        mock_root.return_value = project_root
        mock_migrations.return_value = {}

        _mock_subprocess_ok(mock_subprocess)

        runner = CliRunner()
        result = runner.invoke(cli, ["upgrade", "--version", "1.0.0", "--yes"])
        plain = _strip_ansi(result.output)
        assert "downgrade" in plain.lower()


# ---------------------------------------------------------------------------
# Status version check
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStatusVersionCheck:
    """Tests for the version check in ``dango status``."""

    @patch("dango.cli.helpers.process_manager.get_fastapi_status")
    @patch("dango.platform.local.watcher_lifecycle.get_watcher_status")
    @patch("dango.cli.commands.upgrade.get_latest_version_cached", return_value="2.0.0")
    @patch("dango.__version__", "0.1.0")
    @patch("dango.cli.utils.find_project_root")
    def test_status_shows_update_available(
        self,
        mock_root: MagicMock,
        _cache: MagicMock,
        mock_watcher: MagicMock,
        mock_fastapi: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Status shows update notice when newer version exists."""
        mock_root.return_value = _setup_project(tmp_path)
        mock_watcher.return_value = {"running": False, "pid": None}
        mock_fastapi.return_value = {
            "running": False,
            "pid": None,
            "url": "http://localhost:8800",
            "log_file": tmp_path / "x.log",
        }
        with (
            patch("dango.config.loader.ConfigLoader") as cfg,
            patch("dango.platform.local.network.NetworkConfig") as net,
            patch("dango.platform.local.network.NginxManager") as ngx,
            patch("dango.platform.docker.DockerManager") as dkr,
        ):
            mc = MagicMock()
            mc.project.name = "test"
            mc.platform.auto_sync = False
            cfg.return_value.load_config.return_value = mc
            net.return_value.get_project_info.return_value = None
            ngx.return_value.is_running.return_value = False
            dkr.return_value.get_service_status.return_value = {}
            result = CliRunner().invoke(cli, ["status"])
        plain = _strip_ansi(result.output)
        assert "Update available" in plain
        assert "dango upgrade" in plain
