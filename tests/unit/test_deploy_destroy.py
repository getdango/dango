"""tests/unit/test_deploy_destroy.py

Unit tests for dango/cli/commands/deploy.py (destroy command).

Uses Click's CliRunner with mocked DigitalOcean/SSH clients.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.deploy import deploy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"
_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_DO_CLIENT = "dango.platform.cloud.digitalocean.DigitalOceanClient"
_PATCH_SSH = "dango.platform.cloud.ssh.SSHManager"
_PATCH_SPACES = "dango.platform.cloud.spaces.SpacesClient"


def _make_cloud_config(
    droplet_id=42,
    droplet_ip="1.2.3.4",
    firewall_id="fw-abc",
    ssh_key_id=99,
    ssh_key_path=".dango/cloud_key",
    spaces=None,
):
    cfg = MagicMock()
    cfg.droplet_id = droplet_id
    cfg.droplet_ip = droplet_ip
    cfg.firewall_id = firewall_id
    cfg.ssh_key_id = ssh_key_id
    cfg.ssh_key_path = ssh_key_path
    cfg.region = "nyc1"
    cfg.size = "s-2vcpu-4gb"
    cfg.spaces = spaces
    return cfg


def _make_spaces_config():
    spaces = MagicMock()
    spaces.bucket = "dango-backups"
    spaces.region = "nyc3"
    spaces.access_key_env = "SPACES_ACCESS_KEY"
    spaces.secret_key_env = "SPACES_SECRET_KEY"
    return spaces


_UNSET = object()


def _make_loader(cloud_cfg=_UNSET):
    loader = MagicMock()
    loader.load_cloud_config.return_value = (
        _make_cloud_config() if cloud_cfg is _UNSET else cloud_cfg
    )
    return loader


def _run(args, tmp_path, input_text=None, catch_exceptions=False):
    runner = CliRunner()
    return runner.invoke(
        deploy,
        args,
        obj={"project_root": tmp_path},
        input=input_text,
        catch_exceptions=catch_exceptions,
    )


# ---------------------------------------------------------------------------
# TestDestroyConfirmation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDestroyConfirmation:
    def test_must_type_ip_to_confirm(self, tmp_path):
        """Destroy requires typing the droplet IP to confirm."""
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            # Type correct IP
            result = _run(
                ["destroy"],
                tmp_path,
                input_text="1.2.3.4\n",
                catch_exceptions=True,
            )

        # Should proceed (may fail at deletion step, but shouldn't abort at confirm)
        assert "Aborted" not in result.output

    def test_wrong_ip_aborts(self, tmp_path):
        """Destroy aborts when typed IP doesn't match."""
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(
                ["destroy"],
                tmp_path,
                input_text="wrong-ip\n",
                catch_exceptions=True,
            )

        assert result.exit_code != 0
        assert "Aborted" in result.output

    def test_force_skips_confirmation(self, tmp_path):
        """--force skips confirmation prompt."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            result = _run(["destroy", "--force"], tmp_path, catch_exceptions=True)

        # Should not ask for input — exits 0 or fails at API level
        assert "Type the droplet" not in result.output

    def test_no_deployment_exits(self, tmp_path):
        """Exits when no cloud deployment found."""
        mock_loader = _make_loader(cloud_cfg=None)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
        ):
            result = _run(["destroy"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0
        assert "No cloud deployment" in result.output


# ---------------------------------------------------------------------------
# TestDestroyResourceDeletion
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDestroyResourceDeletion:
    def test_deletes_droplet(self, tmp_path):
        """Destroy deletes the droplet."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            _run(["destroy", "--force"], tmp_path)

        mock_client.delete_droplet.assert_called_once_with(42)

    def test_deletes_firewall(self, tmp_path):
        """Destroy deletes the firewall when firewall_id is set."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            _run(["destroy", "--force"], tmp_path)

        mock_client.delete_firewall.assert_called_once_with("fw-abc")

    def test_deletes_ssh_key_by_id(self, tmp_path):
        """Destroy deletes SSH key using ssh_key_id when available."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            _run(["destroy", "--force"], tmp_path)

        mock_client.delete_ssh_key.assert_called_once_with(99)

    def test_keep_ssh_key_skips_deletion(self, tmp_path):
        """--keep-ssh-key prevents SSH key deletion."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            _run(["destroy", "--force", "--keep-ssh-key"], tmp_path)

        mock_client.delete_ssh_key.assert_not_called()

    def test_cleans_up_cloud_yml(self, tmp_path):
        """Destroy removes .dango/cloud.yml."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            _run(["destroy", "--force"], tmp_path)

        assert not cloud_yml.exists()

    def test_partial_failure_continues(self, tmp_path):
        """Destroy continues when one deletion fails."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        mock_loader = _make_loader()

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.delete_droplet.side_effect = Exception("API error")
            mock_client_cls.return_value = mock_client
            result = _run(["destroy", "--force"], tmp_path)

        # Should still attempt other deletions
        mock_client.delete_firewall.assert_called_once()
        assert "error" in result.output.lower()


# ---------------------------------------------------------------------------
# TestDestroyWithSpaces
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDestroyWithSpaces:
    def test_deletes_spaces_bucket(self, tmp_path):
        """Destroy empties and deletes the Spaces bucket."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        cloud_cfg = _make_cloud_config(spaces=_make_spaces_config())
        mock_loader = _make_loader(cloud_cfg=cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
            patch(_PATCH_SPACES) as mock_spaces_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            mock_spaces = MagicMock()
            mock_spaces.list_objects.return_value = [
                {"Key": "backup1.tar.gz"},
                {"Key": "backup2.tar.gz"},
            ]
            mock_spaces_cls.return_value = mock_spaces

            _run(["destroy", "--force"], tmp_path)

        assert mock_spaces.delete.call_count == 2
        mock_spaces.delete_bucket.assert_called_once()

    def test_keep_spaces_skips_bucket_deletion(self, tmp_path):
        """--keep-spaces prevents bucket deletion."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()
        cloud_cfg = _make_cloud_config(spaces=_make_spaces_config())
        mock_loader = _make_loader(cloud_cfg=cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
            patch(_PATCH_SPACES) as mock_spaces_cls,
        ):
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client

            _run(["destroy", "--force", "--keep-spaces"], tmp_path)

        mock_spaces_cls.assert_not_called()


# ---------------------------------------------------------------------------
# TestDestroySSHKeyFallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDestroySSHKeyFallback:
    def test_fallback_to_fingerprint_match(self, tmp_path):
        """When ssh_key_id is None, falls back to fingerprint matching."""
        cloud_yml = tmp_path / ".dango" / "cloud.yml"
        cloud_yml.parent.mkdir(parents=True, exist_ok=True)
        cloud_yml.touch()

        # Create a mock SSH public key
        key_dir = tmp_path / ".dango"
        key_dir.mkdir(parents=True, exist_ok=True)
        pub_key = key_dir / "cloud_key.pub"
        # Write a valid-looking SSH public key
        pub_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIG3x9mnop dango@test")

        cloud_cfg = _make_cloud_config(ssh_key_id=None)
        mock_loader = _make_loader(cloud_cfg=cloud_cfg)

        with (
            patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
            patch(_PATCH_LOADER, return_value=mock_loader),
            patch(_PATCH_DO_CLIENT) as mock_client_cls,
            patch("dango.cli.commands.deploy._compute_ssh_fingerprint", return_value="aa:bb:cc"),
        ):
            mock_client = MagicMock()
            mock_client.list_ssh_keys.return_value = [
                {"id": 123, "fingerprint": "aa:bb:cc"},
            ]
            mock_client_cls.return_value = mock_client

            _run(["destroy", "--force"], tmp_path)

        mock_client.delete_ssh_key.assert_called_once_with(123)
