"""tests/unit/test_remote_cli.py

Unit tests for dango/cli/commands/remote.py.

Uses Click's CliRunner with mocked DigitalOceanClient and ConfigLoader
to avoid any real network or filesystem access.

Patching note: ``remote.py`` uses lazy imports inside function bodies, so
patches must target the canonical module paths (e.g.
``dango.config.loader.ConfigLoader``), not the remote module itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.exceptions import CloudAPIError, CloudError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PUBLIC_SOURCES = {"addresses": ["0.0.0.0/0", "::/0"]}
_SSH_RULE = {"protocol": "tcp", "ports": "22", "sources": _PUBLIC_SOURCES}
_HTTP_RULE = {"protocol": "tcp", "ports": "80", "sources": _PUBLIC_SOURCES}
_HTTPS_RULE = {"protocol": "tcp", "ports": "443", "sources": _PUBLIC_SOURCES}
_OUTBOUND_RULE = {"protocol": "tcp", "ports": "all", "destinations": _PUBLIC_SOURCES}


def _make_firewall_dict(name: str = "dango-fw-42") -> dict[str, Any]:
    return {
        "id": "fw-abc",
        "name": name,
        "inbound_rules": [_SSH_RULE, _HTTP_RULE, _HTTPS_RULE],
        "outbound_rules": [_OUTBOUND_RULE],
        "droplet_ids": [42],
        "tags": [],
    }


def _make_cloud_config(firewall_id: str | None = "fw-abc") -> MagicMock:
    """Return a mock CloudConfig object."""
    cfg = MagicMock()
    cfg.droplet_id = 42
    cfg.droplet_ip = "1.2.3.4"
    cfg.firewall_id = firewall_id
    return cfg


def _make_loader(cloud_cfg: MagicMock | None = None) -> MagicMock:
    """Return a mock ConfigLoader instance."""
    loader = MagicMock()
    loader.load_cloud_config.return_value = cloud_cfg or _make_cloud_config()
    return loader


# Patch paths — lazy imports resolved at call time, patch at definition site
_PATCH_LOADER = "dango.config.loader.ConfigLoader"
_PATCH_CLIENT = "dango.platform.cloud.digitalocean.DigitalOceanClient"
_PATCH_REQUIRE_CTX = "dango.cli.utils.require_project_context"


def _run(args: list[str], tmp_path: Path, *, catch_exceptions: bool = False) -> Any:
    """Invoke ``remote`` CLI group with the given args."""
    runner = CliRunner()
    return runner.invoke(
        remote,
        args,
        obj={"project_root": tmp_path},
        catch_exceptions=catch_exceptions,
    )


# ---------------------------------------------------------------------------
# 1. firewall list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirewallListCommand:
    def test_shows_firewall_rules_table(self, tmp_path: Path):
        """firewall list displays a Rich table of rules."""
        fw = _make_firewall_dict()
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.get_firewall.return_value = fw
                    mock_client_cls.return_value = mock_client

                    result = _run(["firewall", "list"], tmp_path, catch_exceptions=False)

        assert result.exit_code == 0
        assert "dango-fw-42" in result.output

    def test_no_deployment_exits_with_error(self, tmp_path: Path):
        """Exits when no cloud deployment is configured."""
        mock_loader_instance = _make_loader(cloud_cfg=None)

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                result = _run(["firewall", "list"], tmp_path, catch_exceptions=True)

        # Either exits non-zero, or the output contains an error message
        assert result.exit_code != 0 or "No cloud deployment" in result.output

    def test_no_firewall_configured_exits_with_error(self, tmp_path: Path):
        """Exits when firewall_id is not set in cloud config."""
        mock_loader_instance = _make_loader(cloud_cfg=_make_cloud_config(firewall_id=None))

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                result = _run(["firewall", "list"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0 or "No firewall" in result.output

    def test_api_error_exits_with_message(self, tmp_path: Path):
        """API error from get_firewall is shown as a user-facing error."""
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.get_firewall.side_effect = CloudAPIError(
                        "Not found", status_code=404
                    )
                    mock_client_cls.return_value = mock_client

                    result = _run(["firewall", "list"], tmp_path, catch_exceptions=True)

        assert result.exit_code != 0 or "Error" in result.output


# ---------------------------------------------------------------------------
# 2. firewall allow-ip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirewallAllowIpCommand:
    def test_valid_ip_succeeds(self, tmp_path: Path):
        """allow-ip with a valid IP prints a success message."""
        fw = _make_firewall_dict()
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.get_firewall.return_value = fw
                    mock_client.update_firewall.return_value = fw
                    mock_client_cls.return_value = mock_client

                    result = _run(
                        ["firewall", "allow-ip", "203.0.113.42"],
                        tmp_path,
                        catch_exceptions=False,
                    )

        assert result.exit_code == 0
        assert "203.0.113.42" in result.output

    def test_valid_cidr_succeeds(self, tmp_path: Path):
        """allow-ip with a CIDR range succeeds."""
        fw = _make_firewall_dict()
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.get_firewall.return_value = fw
                    mock_client.update_firewall.return_value = fw
                    mock_client_cls.return_value = mock_client

                    result = _run(
                        ["firewall", "allow-ip", "203.0.113.0/24"],
                        tmp_path,
                        catch_exceptions=False,
                    )

        assert result.exit_code == 0

    def test_invalid_ip_exits_with_error(self, tmp_path: Path):
        """allow-ip with an invalid IP prints an error and exits non-zero."""
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    # validate_ip_or_cidr is called before get_firewall, raises immediately
                    mock_client.get_firewall.side_effect = CloudError(
                        "Invalid IP", error_code="DANGO-D020"
                    )
                    mock_client_cls.return_value = mock_client

                    result = _run(
                        ["firewall", "allow-ip", "not-an-ip"],
                        tmp_path,
                        catch_exceptions=True,
                    )

        assert result.exit_code != 0 or "Error" in result.output


# ---------------------------------------------------------------------------
# 3. firewall allow-all
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirewallAllowAllCommand:
    def test_reverts_to_public_access(self, tmp_path: Path):
        """allow-all prints a success message on success."""
        fw = _make_firewall_dict()
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.get_firewall.return_value = fw
                    mock_client.update_firewall.return_value = fw
                    mock_client_cls.return_value = mock_client

                    result = _run(
                        ["firewall", "allow-all"],
                        tmp_path,
                        catch_exceptions=False,
                    )

        assert result.exit_code == 0
        assert "open to all" in result.output

    def test_api_error_shows_message(self, tmp_path: Path):
        """API error during allow-all is shown as a user-facing error."""
        mock_loader_instance = _make_loader()

        with patch(_PATCH_REQUIRE_CTX, return_value=tmp_path):
            with patch(_PATCH_LOADER, return_value=mock_loader_instance):
                with patch(_PATCH_CLIENT) as mock_client_cls:
                    mock_client = MagicMock()
                    mock_client.get_firewall.side_effect = CloudAPIError(
                        "Server error", status_code=500
                    )
                    mock_client_cls.return_value = mock_client

                    result = _run(
                        ["firewall", "allow-all"],
                        tmp_path,
                        catch_exceptions=True,
                    )

        assert result.exit_code != 0 or "Error" in result.output
