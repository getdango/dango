"""tests/unit/test_resize.py

Unit tests for dango/platform/cloud/resize.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError
from dango.platform.cloud.ssh import CommandResult

# Patch paths — lazy imports, patch at source.
_PATCH_BACKUP = "dango.platform.cloud.backup.create_backup"
_PATCH_STOP = "dango.platform.cloud.backup.stop_services"
_PATCH_START = "dango.platform.cloud.backup.start_services"
_PATCH_VERIFY = "dango.platform.cloud.backup.verify_health"
_PATCH_SAVE_META = "dango.platform.cloud.provisioning.save_provisioning_metadata"
_PATCH_WAIT_DROPLET = "dango.platform.cloud.provisioning.wait_for_droplet_ready"
_PATCH_WAIT_SSH = "dango.platform.cloud.provisioning.wait_for_ssh"


def _make_ssh_mock(
    *,
    exec_results: dict[str, tuple[str, str, int]] | None = None,
) -> MagicMock:
    """Return a mock SSHManager with configurable exec_command results."""
    results = exec_results or {}

    def _exec_side_effect(command: str, timeout: int | None = None) -> CommandResult:
        for substr, (stdout, stderr, exit_code) in results.items():
            if substr in command:
                return CommandResult(stdout=stdout, stderr=stderr, exit_code=exit_code)
        return CommandResult(stdout="", stderr="", exit_code=0)

    ssh = MagicMock()
    ssh.exec_command.side_effect = _exec_side_effect
    ssh.write_remote_file = MagicMock()
    ssh.connect = MagicMock()
    ssh.disconnect = MagicMock()
    return ssh


def _make_droplet(
    size_slug: str = "s-2vcpu-4gb",
    ip: str = "1.2.3.4",
) -> dict[str, Any]:
    """Return a minimal droplet dict."""
    return {
        "id": 42,
        "size_slug": size_slug,
        "networks": {
            "v4": [
                {"ip_address": ip, "type": "public"},
            ]
        },
    }


def _make_do_client(droplet: dict[str, Any] | None = None) -> MagicMock:
    """Return a mock DigitalOceanClient."""
    client = MagicMock()
    client.get_droplet.return_value = droplet or _make_droplet()
    client.power_off.return_value = {"id": 100}
    client.resize.return_value = {"id": 101}
    client.power_on.return_value = {"id": 102}
    client.wait_for_action.return_value = {"id": 100, "status": "completed"}
    return client


@pytest.mark.unit
class TestValidateSizeSlug:
    """Tests for validate_size_slug()."""

    def test_valid_slugs(self) -> None:
        from dango.platform.cloud.resize import validate_size_slug

        validate_size_slug("s-2vcpu-4gb")
        validate_size_slug("s-1vcpu-2gb")
        validate_size_slug("g-4vcpu-16gb")
        validate_size_slug("so1_5-2vcpu-16gb")

    def test_invalid_slugs(self) -> None:
        from dango.platform.cloud.resize import validate_size_slug

        with pytest.raises(CloudError, match="Invalid size slug"):
            validate_size_slug("")

        with pytest.raises(CloudError, match="Invalid size slug"):
            validate_size_slug("s-2vcpu-4gb; rm -rf /")

        with pytest.raises(CloudError, match="Invalid size slug"):
            validate_size_slug("$(whoami)")

        with pytest.raises(CloudError, match="Invalid size slug"):
            validate_size_slug("size with spaces")


@pytest.mark.unit
class TestGetDiskWarning:
    """Tests for get_disk_warning()."""

    def test_downgrade_warns(self) -> None:
        from dango.platform.cloud.resize import get_disk_warning

        warning = get_disk_warning("s-4vcpu-8gb", "s-1vcpu-2gb")
        assert warning is not None
        assert "does not shrink" in warning

    def test_upgrade_no_warning(self) -> None:
        from dango.platform.cloud.resize import get_disk_warning

        warning = get_disk_warning("s-1vcpu-2gb", "s-4vcpu-8gb")
        assert warning is None

    def test_same_size_no_warning(self) -> None:
        from dango.platform.cloud.resize import get_disk_warning

        warning = get_disk_warning("s-2vcpu-4gb", "s-2vcpu-4gb")
        assert warning is None

    def test_custom_slug_returns_none(self) -> None:
        from dango.platform.cloud.resize import get_disk_warning

        warning = get_disk_warning("custom-slug", "s-2vcpu-4gb")
        assert warning is None


@pytest.mark.unit
class TestGenerateDbtProfilesYml:
    """Tests for generate_dbt_profiles_yml()."""

    def test_default_generation(self) -> None:
        from dango.platform.cloud.resize import generate_dbt_profiles_yml

        content = generate_dbt_profiles_yml("my_project", 4, 8)
        assert "my_project:" in content
        assert "threads: 4" in content
        assert "memory_limit: 2GB" in content
        assert "/srv/dango/project/data/warehouse.duckdb" in content

    def test_with_overrides(self) -> None:
        from dango.platform.cloud.resize import generate_dbt_profiles_yml

        overrides = MagicMock()
        overrides.threads = 8
        overrides.memory_limit = "6GB"

        content = generate_dbt_profiles_yml("my_project", 4, 16, overrides)
        assert "threads: 8" in content
        assert "memory_limit: 6GB" in content

    def test_minimum_memory(self) -> None:
        from dango.platform.cloud.resize import generate_dbt_profiles_yml

        content = generate_dbt_profiles_yml("proj", 1, 2)
        assert "memory_limit: 1GB" in content


@pytest.mark.unit
class TestRegenerateDbtProfiles:
    """Tests for regenerate_dbt_profiles()."""

    def test_writes_profiles(self) -> None:
        from dango.platform.cloud.resize import regenerate_dbt_profiles

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": (
                    "name: my_project\nversion: 1.0.0",
                    "",
                    0,
                ),
            }
        )

        result = regenerate_dbt_profiles(ssh, "s-4vcpu-8gb")
        assert result is True
        ssh.write_remote_file.assert_called_once()
        content = ssh.write_remote_file.call_args[0][1]
        assert "my_project:" in content

    def test_returns_false_when_no_dbt_project(self) -> None:
        from dango.platform.cloud.resize import regenerate_dbt_profiles

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": ("", "", 1),
            }
        )

        result = regenerate_dbt_profiles(ssh, "s-2vcpu-4gb")
        assert result is False

    def test_custom_slug_uses_defaults(self) -> None:
        from dango.platform.cloud.resize import regenerate_dbt_profiles

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": (
                    "name: proj\nversion: 1.0.0",
                    "",
                    0,
                ),
            }
        )

        regenerate_dbt_profiles(ssh, "custom-unknown-slug")
        content = ssh.write_remote_file.call_args[0][1]
        assert "threads: 2" in content


@pytest.mark.unit
class TestResizeDroplet:
    """Tests for resize_droplet()."""

    def test_full_resize_workflow(self) -> None:
        from dango.platform.cloud.resize import resize_droplet

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": (
                    "name: proj\nversion: 1.0.0",
                    "",
                    0,
                ),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
                "docker compose": ("", "", 0),
            }
        )
        client = _make_do_client()

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with (
            patch(_PATCH_BACKUP, return_value=backup_result) as mock_backup,
            patch(_PATCH_WAIT_DROPLET),
            patch(_PATCH_WAIT_SSH),
            patch(_PATCH_START),
        ):
            result = resize_droplet(client, ssh, 42, "s-4vcpu-8gb")

        assert result.old_size == "s-2vcpu-4gb"
        assert result.new_size == "s-4vcpu-8gb"
        assert result.backup_path is not None
        assert result.dbt_profiles_regenerated is True

        client.power_off.assert_called_once_with(42)
        client.resize.assert_called_once_with(42, "s-4vcpu-8gb")
        client.power_on.assert_called_once_with(42)
        assert client.wait_for_action.call_count == 3

        mock_backup.assert_called_once()
        _, kwargs = mock_backup.call_args
        assert kwargs["restart_services"] is False

    def test_progress_callback(self) -> None:
        from dango.platform.cloud.resize import resize_droplet

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": (
                    "name: proj",
                    "",
                    0,
                ),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
                "docker compose": ("", "", 0),
            }
        )
        client = _make_do_client()

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        progress_calls: list[tuple[str, str]] = []

        with (
            patch(_PATCH_BACKUP, return_value=backup_result),
            patch(_PATCH_WAIT_DROPLET),
            patch(_PATCH_WAIT_SSH),
            patch(_PATCH_START),
        ):
            resize_droplet(
                client,
                ssh,
                42,
                "s-4vcpu-8gb",
                on_progress=lambda s, st: progress_calls.append((s, st)),
            )

        steps = [s for s, _ in progress_calls]
        assert "backup" in steps
        assert "power_off" in steps
        assert "resize" in steps
        assert "power_on" in steps
        assert "verify_health" in steps

    def test_action_error_attempts_power_on(self) -> None:
        from dango.platform.cloud.resize import resize_droplet

        ssh = _make_ssh_mock()
        client = _make_do_client()
        client.wait_for_action.side_effect = [
            {"id": 100, "status": "completed"},  # power_off OK
            CloudError("Action errored"),  # resize fails
        ]

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with (
            patch(_PATCH_BACKUP, return_value=backup_result),
            pytest.raises(CloudError, match="Action errored"),
        ):
            resize_droplet(client, ssh, 42, "s-4vcpu-8gb")

        # Should attempt to power on after failure
        assert client.power_on.call_count >= 1

    def test_resize_without_backup(self) -> None:
        from dango.platform.cloud.resize import resize_droplet

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": (
                    "name: proj\nversion: 1.0.0",
                    "",
                    0,
                ),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
                "docker compose": ("", "", 0),
            }
        )
        client = _make_do_client()

        with (
            patch(_PATCH_STOP) as mock_stop,
            patch(_PATCH_WAIT_DROPLET),
            patch(_PATCH_WAIT_SSH),
            patch(_PATCH_START),
            patch(_PATCH_VERIFY, return_value=True),
        ):
            result = resize_droplet(client, ssh, 42, "s-4vcpu-8gb", create_backup=False)

        assert result.backup_path is None
        mock_stop.assert_called_once()

    def test_resize_persists_metadata(self, tmp_path: Any) -> None:
        from dango.platform.cloud.resize import resize_droplet

        ssh = _make_ssh_mock(
            exec_results={
                "cat /srv/dango/project/dbt/dbt_project.yml": (
                    "name: proj\nversion: 1.0.0",
                    "",
                    0,
                ),
                "curl -sf": ("ok", "", 0),
                "systemctl": ("", "", 0),
                "docker compose": ("", "", 0),
            }
        )
        client = _make_do_client()

        backup_result = MagicMock()
        backup_result.archive_path = "/srv/dango/backups/deploy/backup-test.tar.gz"

        with (
            patch(_PATCH_BACKUP, return_value=backup_result),
            patch(_PATCH_WAIT_DROPLET),
            patch(_PATCH_WAIT_SSH),
            patch(_PATCH_START),
            patch(_PATCH_VERIFY, return_value=True),
            patch(_PATCH_SAVE_META) as mock_save,
        ):
            resize_droplet(
                client,
                ssh,
                42,
                "s-4vcpu-8gb",
                project_root=tmp_path,
                region="nyc1",
            )

        mock_save.assert_called_once()
        kwargs = mock_save.call_args[1]
        assert kwargs["size"] == "s-4vcpu-8gb"
        assert kwargs["region"] == "nyc1"
