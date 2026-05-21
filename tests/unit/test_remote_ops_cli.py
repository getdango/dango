"""tests/unit/test_remote_ops_cli.py

Unit tests for dango/cli/commands/remote_ops.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[^m]*m", "", text)


_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"
_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_SSH_CLS = "dango.platform.cloud.ssh.SSHManager"


def _make_cloud_config(
    droplet_id: int = 42,
    droplet_ip: str = "1.2.3.4",
    size: str = "s-2vcpu-4gb",
    region: str = "nyc1",
    domain: str | None = None,
    firewall_id: str | None = "fw-abc",
    spaces: Any | None = "default",
    dbt_overrides: Any | None = None,
    ssh_key_path: str = ".dango/cloud_key",
    ssh_key_id: int | None = 100,
) -> MagicMock:
    """Return a mock CloudConfig."""
    cfg = MagicMock()
    cfg.droplet_id = droplet_id
    cfg.droplet_ip = droplet_ip
    cfg.size = size
    cfg.region = region
    cfg.domain = domain
    cfg.firewall_id = firewall_id
    cfg.ssh_key_path = ssh_key_path
    cfg.ssh_key_id = ssh_key_id
    cfg.dbt_overrides = dbt_overrides
    if spaces == "default":
        s = MagicMock()
        s.bucket = "test-bucket"
        s.region = "nyc3"
        cfg.spaces = s
    else:
        cfg.spaces = spaces
    return cfg


def _make_loader(cloud_cfg: Any | None = None) -> MagicMock:
    """Return a mock ConfigLoader."""
    loader = MagicMock()
    loader.load_cloud_config.return_value = cloud_cfg or _make_cloud_config()
    return loader


def _make_ssh_mock() -> MagicMock:
    """Return a mock SSHManager."""
    ssh = MagicMock()
    ssh.connect = MagicMock()
    ssh.disconnect = MagicMock()
    return ssh


def _run(
    args: list[str],
    tmp_path: Path,
    *,
    catch_exceptions: bool = False,
) -> Any:
    """Invoke ``remote`` CLI group with the given args."""
    runner = CliRunner()
    return runner.invoke(
        remote,
        args,
        obj={"project_root": tmp_path},
        catch_exceptions=catch_exceptions,
    )


# ---------------------------------------------------------------------------
# dango remote upgrade
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteUpgradeCommand:
    """Tests for the ``dango remote upgrade`` command."""

    def test_shows_version_comparison(self, tmp_path: Path) -> None:
        """Upgrade shows current vs latest before confirming."""
        cloud_cfg = _make_cloud_config()
        loader = _make_loader(cloud_cfg)
        ssh = _make_ssh_mock()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
            patch(_PATCH_SSH_CLS, return_value=ssh),
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version",
                return_value="1.1.0",
            ),
            patch(
                "dango.platform.cloud.server_status._get_dango_version",
                return_value="1.0.0",
            ),
        ):
            result = _run(["upgrade"], tmp_path, catch_exceptions=True)

        plain = _strip_ansi(result.output)
        assert "1.0.0" in plain
        assert "1.1.0" in plain

    def test_already_at_latest_skips(self, tmp_path: Path) -> None:
        """Upgrade exits early when already at target."""
        cloud_cfg = _make_cloud_config()
        loader = _make_loader(cloud_cfg)
        ssh = _make_ssh_mock()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
            patch(_PATCH_SSH_CLS, return_value=ssh),
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version",
                return_value="1.1.0",
            ),
            patch(
                "dango.platform.cloud.server_status._get_dango_version",
                return_value="1.1.0",
            ),
        ):
            result = _run(["upgrade"], tmp_path, catch_exceptions=True)

        assert "no upgrade needed" in result.output.lower()

    def test_invalid_version_rejected(self, tmp_path: Path) -> None:
        """Invalid --version is caught before SSH."""
        cloud_cfg = _make_cloud_config()
        loader = _make_loader(cloud_cfg)
        ssh = _make_ssh_mock()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
            patch(_PATCH_SSH_CLS, return_value=ssh),
        ):
            result = _run(
                ["upgrade", "--version", "bad-version"],
                tmp_path,
                catch_exceptions=True,
            )

        assert result.exit_code != 0

    def test_yes_flag_skips_confirmation(self, tmp_path: Path) -> None:
        """--yes skips the confirmation prompt."""
        from dango.platform.cloud.upgrade import UpgradeResult

        cloud_cfg = _make_cloud_config()
        loader = _make_loader(cloud_cfg)
        ssh = _make_ssh_mock()

        upgrade_result = UpgradeResult(
            old_version="1.0.0",
            new_version="1.1.0",
            backup_path="/srv/dango/backups/deploy/backup.tar.gz",
            migrations_run=True,
            docker_rebuilt=True,
            health_check_passed=True,
            duration_seconds=45.0,
        )

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
            patch(_PATCH_SSH_CLS, return_value=ssh),
            patch(
                "dango.platform.cloud.server_status.check_latest_pypi_version",
                return_value="1.1.0",
            ),
            patch(
                "dango.platform.cloud.server_status._get_dango_version",
                return_value="1.0.0",
            ),
            patch(
                "dango.platform.cloud.upgrade.upgrade_dango",
                return_value=upgrade_result,
            ),
        ):
            result = _run(["upgrade", "--yes"], tmp_path, catch_exceptions=True)

        assert "Upgrade complete" in result.output


# ---------------------------------------------------------------------------
# dango remote resize
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteResizeCommand:
    """Tests for the ``dango remote resize`` command."""

    def test_no_arg_shows_size_list(self, tmp_path: Path) -> None:
        """resize with no arg shows current spec and tier table."""
        cloud_cfg = _make_cloud_config(size="s-2vcpu-4gb")
        loader = _make_loader(cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
        ):
            result = _run(["resize"], tmp_path, catch_exceptions=True)

        assert result.exit_code == 0
        assert "Standard" in result.output
        assert "s-2vcpu-4gb" in result.output
        assert "Performance" in result.output

    def test_invalid_slug_rejected(self, tmp_path: Path) -> None:
        """Invalid size slug is caught."""
        cloud_cfg = _make_cloud_config()
        loader = _make_loader(cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
        ):
            result = _run(
                ["resize", "bad slug"],
                tmp_path,
                catch_exceptions=True,
            )

        assert result.exit_code != 0

    def test_resize_with_arg_shows_plan(self, tmp_path: Path) -> None:
        """resize with arg shows comparison and asks for confirmation."""
        cloud_cfg = _make_cloud_config(size="s-2vcpu-4gb")
        loader = _make_loader(cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
        ):
            result = _run(
                ["resize", "s-4vcpu-8gb"],
                tmp_path,
                catch_exceptions=True,
            )

        assert "Resize plan" in result.output
        assert "s-4vcpu-8gb" in result.output

    def test_yes_flag_runs_resize(self, tmp_path: Path) -> None:
        """--yes skips the confirmation prompt."""
        from dango.platform.cloud.resize import ResizeResult

        cloud_cfg = _make_cloud_config(size="s-2vcpu-4gb")
        loader = _make_loader(cloud_cfg)
        ssh = _make_ssh_mock()

        resize_result = ResizeResult(
            old_size="s-2vcpu-4gb",
            new_size="s-4vcpu-8gb",
            old_tier="Standard",
            new_tier="Performance",
            duration_seconds=120.0,
            backup_path="/srv/dango/backups/deploy/backup.tar.gz",
            dbt_profiles_regenerated=True,
        )

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
            patch(_PATCH_SSH_CLS, return_value=ssh),
            patch(
                "dango.platform.cloud.resize.resize_droplet",
                return_value=resize_result,
            ),
            patch(
                "dango.platform.cloud.digitalocean.DigitalOceanClient",
                return_value=MagicMock(),
            ),
        ):
            result = _run(
                ["resize", "s-4vcpu-8gb", "--yes"],
                tmp_path,
                catch_exceptions=True,
            )

        assert "Resize complete" in result.output


# ---------------------------------------------------------------------------
# dango remote migrate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRemoteMigrateCommand:
    """Tests for the ``dango remote migrate`` command."""

    def test_requires_size_option(self, tmp_path: Path) -> None:
        """migrate requires --size."""
        result = _run(["migrate"], tmp_path, catch_exceptions=True)
        assert result.exit_code != 0

    def test_no_spaces_shows_error(self, tmp_path: Path) -> None:
        """migrate without Spaces configured shows error."""
        cloud_cfg = _make_cloud_config(spaces=None)
        loader = _make_loader(cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
        ):
            result = _run(
                ["migrate", "--size", "s-4vcpu-8gb"],
                tmp_path,
                catch_exceptions=True,
            )

        assert result.exit_code != 0
        assert "requires Spaces" in result.output

    def test_shows_migration_plan(self, tmp_path: Path) -> None:
        """migrate shows current → new comparison."""
        cloud_cfg = _make_cloud_config(size="s-2vcpu-4gb")
        loader = _make_loader(cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
        ):
            result = _run(
                ["migrate", "--size", "s-4vcpu-8gb"],
                tmp_path,
                catch_exceptions=True,
            )

        assert "Migration plan" in result.output

    def test_yes_flag_runs_migration(self, tmp_path: Path) -> None:
        """--yes skips confirmation."""
        from dango.platform.cloud.migrate import MigrateResult

        cloud_cfg = _make_cloud_config()
        loader = _make_loader(cloud_cfg)
        ssh = _make_ssh_mock()

        migrate_result = MigrateResult(
            old_droplet_id=42,
            new_droplet_id=99,
            new_droplet_ip="5.6.7.8",
            new_region="nyc1",
            new_size="s-4vcpu-8gb",
            duration_seconds=300.0,
            old_droplet_destroyed=True,
            dns_updated=False,
        )

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
            patch(
                "dango.platform.cloud.digitalocean.DigitalOceanClient",
                return_value=MagicMock(),
            ),
            patch(_PATCH_SSH_CLS, return_value=ssh),
            patch(
                "dango.platform.cloud.migrate.migrate_server",
                return_value=migrate_result,
            ),
        ):
            result = _run(
                ["migrate", "--size", "s-4vcpu-8gb", "--yes"],
                tmp_path,
                catch_exceptions=True,
            )

        assert "Migration complete" in result.output

    def test_region_shows_in_plan(self, tmp_path: Path) -> None:
        """--region shows target region in migration plan."""
        cloud_cfg = _make_cloud_config(size="s-2vcpu-4gb")
        loader = _make_loader(cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=loader),
        ):
            result = _run(
                ["migrate", "--size", "s-4vcpu-8gb", "--region", "sfo3"],
                tmp_path,
                catch_exceptions=True,
            )

        assert "sfo3" in result.output
