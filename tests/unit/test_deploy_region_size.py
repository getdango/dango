"""tests/unit/test_deploy_region_size.py

T16 failure-mode matrix: the deploy wizard and provisioning must not offer or
use a region where the chosen Droplet size does not exist (recorded API data).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_wizard
from dango.exceptions import CloudError, CloudProvisioningError

FIXTURE = Path(__file__).parent.parent / "fixtures" / "do_sizes_s_2vcpu_4gb.json"
ALLOWED = {"ams3", "blr1", "fra1", "lon1", "sfo2", "sgp1", "syd1", "tor1"}


@pytest.fixture()
def sizes() -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return list(data["sizes"])


def _us_east_clock() -> Any:
    """Patch the clock so the nearest region by UTC offset is nyc (UTC-5)."""
    return patch.multiple(
        "dango.platform.cloud.provisioning.time",
        localtime=MagicMock(return_value=MagicMock(tm_isdst=0)),
        timezone=18000,
    )


def _run_region(allowed: set[str] | None, capsys: Any) -> tuple[str, str, str | None]:
    """Run _step_region accepting the default; return (slug, output, prompt default)."""
    seen: dict[str, str] = {}

    def fake_prompt(text: str, default: str | None = None, **kw: Any) -> str:
        seen["default"] = str(default)
        return str(default)

    with _us_east_clock(), patch.object(deploy_wizard.click, "prompt", fake_prompt):
        slug = deploy_wizard._step_region(allowed)
    out = re.sub(r"\x1b\[[0-9;]*m", "", capsys.readouterr().out)
    return slug, re.sub(r"\s+", " ", out), seen.get("default")


@pytest.mark.unit
class TestWizardRegions:
    def test_row1_excludes_unavailable_and_suggests_nearest_allowed(self, capsys):
        slug, out, default = _run_region(ALLOWED, capsys)
        assert "(nyc1)" not in out and "(nyc3)" not in out
        assert "(tor1)" in out
        # UTC-5 timezone: tor1 (-5) is the nearest of the allowed regions
        assert default == "tor1" and slug == "tor1"

    def test_row2_all_allowed_matches_static_list(self, capsys):
        from dango.platform.cloud.provisioning import list_regions

        every = {r.slug for r in list_regions()}
        slug, out, _ = _run_region(every, capsys)
        assert all(f"({r.slug})" in out for r in list_regions())
        assert slug == "nyc1"  # same suggestion as before the change

    def test_row3_unverified_falls_back_with_warning(self, capsys):
        slug, out, _ = _run_region(None, capsys)
        assert "Could not verify size availability" in out
        assert "(nyc1)" in out and slug == "nyc1"

    def test_row6_unknown_api_region_not_offered(self, capsys):
        _, out, _ = _run_region(ALLOWED | {"zzz9"}, capsys)
        assert "zzz9" not in out


@pytest.mark.unit
class TestWizardCustomSize:
    def test_row7_unknown_custom_slug_reprompts(self, sizes, capsys):
        answers = iter(["3", "s-99vcpu-1tb", "s-2vcpu-4gb"])
        with (
            patch.object(deploy_wizard.click, "prompt", lambda *a, **k: next(answers)),
            patch.object(deploy_wizard, "_safe_confirm", return_value=True) as confirm,
        ):
            slug, _tier = deploy_wizard._step_size(sizes)
        assert slug == "s-2vcpu-4gb"
        assert confirm.call_count == 1  # "Try again?" asked once
        assert "not offered by DigitalOcean" in re.sub(
            r"\x1b\[[0-9;]*m", "", capsys.readouterr().out
        )

    def test_row7_declining_retry_uses_default(self, sizes):
        answers = iter(["3", "s-99vcpu-1tb"])
        with (
            patch.object(deploy_wizard.click, "prompt", lambda *a, **k: next(answers)),
            patch.object(deploy_wizard, "_safe_confirm", return_value=False),
        ):
            slug, _ = deploy_wizard._step_size(sizes)
        assert slug == "s-2vcpu-4gb"

    def test_unverified_custom_slug_accepted(self):
        answers = iter(["3", "s-8vcpu-16gb"])
        with patch.object(deploy_wizard.click, "prompt", lambda *a, **k: next(answers)):
            slug, _ = deploy_wizard._step_size(None)
        assert slug == "s-8vcpu-16gb"


def _config(region: str) -> Any:
    return deploy_wizard.WizardConfig(
        region=region,
        size_slug="s-2vcpu-4gb",
        size_tier=None,
        domain=None,
        admin_email="admin@test.com",
        admin_password="strongpassword123",
        skip_oauth=True,
        enable_backups=False,
        monthly_cost=24,
    )


@pytest.mark.unit
class TestProvisioningPreCheck:
    def _run(self, project_root: Path, region: str, client: MagicMock) -> MagicMock:
        from dango.cli.commands.deploy_provision import run_provisioning

        mock_ssh = MagicMock()
        mock_ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA"
        with (
            patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=mock_ssh),
            patch(
                "dango.platform.cloud.provisioning.provision_droplet",
                side_effect=CloudError("stop here"),
            ) as prov,
            patch("dango.platform.cloud.firewall.create_default_firewall") as fw,
        ):
            with pytest.raises(CloudProvisioningError) as exc:
                run_provisioning(project_root, _config(region))
        self.prov, self.fw, self.ssh, self.exc = prov, fw, mock_ssh, exc
        return client

    def test_row4_unavailable_creates_nothing(self, tmp_path, sizes):
        client = MagicMock()
        client.list_sizes.return_value = sizes
        self._run(tmp_path, "nyc1", client)
        client.upload_ssh_key.assert_not_called()
        client.create_droplet.assert_not_called()
        self.prov.assert_not_called()
        self.fw.assert_not_called()
        self.ssh.generate_key_pair.assert_not_called()
        msg = str(self.exc.value)
        assert "nyc1" in msg and all(r in msg for r in ALLOWED)

    def test_row5_available_proceeds_in_same_order(self, tmp_path, sizes):
        client = MagicMock()
        client.list_sizes.return_value = sizes
        client.upload_ssh_key.return_value = {"id": 1}
        self._run(tmp_path, "sgp1", client)
        # Passed the check: key uploaded, then droplet provisioning attempted
        client.upload_ssh_key.assert_called_once()
        self.prov.assert_called_once()
        assert self.prov.call_args.kwargs["region"] == "sgp1"
        self.fw.assert_not_called()  # provision_droplet raised before firewall

    def test_row3_check_failure_continues(self, tmp_path):
        client = MagicMock()
        client.list_sizes.side_effect = CloudError("network down")
        client.upload_ssh_key.return_value = {"id": 1}
        self._run(tmp_path, "nyc1", client)
        client.upload_ssh_key.assert_called_once()
        self.prov.assert_called_once()
