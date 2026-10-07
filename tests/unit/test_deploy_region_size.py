"""tests/unit/test_deploy_region_size.py

T16 failure-mode matrix: the deploy wizard and provisioning must not offer or
use a region where the chosen Droplet size does not exist (recorded API data).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_wizard
from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud.digitalocean import DigitalOceanClient

FIXTURE = Path(__file__).parent.parent / "fixtures" / "do_sizes_s_2vcpu_4gb.json"
ALLOWED = {"ams3", "blr1", "fra1", "lon1", "sfo2", "sgp1", "syd1", "tor1"}
CUSTOM_REGIONS = ["ams3", "sgp1"]


@pytest.fixture()
def sizes() -> list[dict[str, Any]]:
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    return list(data["sizes"])


def _clock(utc_offset_hours: int) -> Any:
    """Fake clock namespace (not the process-global time module)."""
    fake = SimpleNamespace(
        localtime=lambda: SimpleNamespace(tm_isdst=0),
        timezone=-utc_offset_hours * 3600,
        altzone=-utc_offset_hours * 3600,
    )
    return patch("dango.platform.cloud.provisioning.time", new=fake)


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", text))


def _run_region(allowed: set[str] | None, capsys: Any, utc: int = -5) -> tuple[str, str, str]:
    seen: dict[str, str] = {}

    def fake_prompt(text: str, default: str | None = None, **kw: Any) -> str:
        seen["default"] = str(default)
        return str(default)

    with _clock(utc), patch.object(deploy_wizard.click, "prompt", fake_prompt):
        slug = deploy_wizard._step_region(allowed)
    return slug, _plain(capsys.readouterr().out), seen["default"]


@pytest.mark.unit
class TestWizardRegions:
    def test_row1_excludes_unavailable_and_suggests_nearest_allowed(self, capsys):
        slug, out, default = _run_region(ALLOWED, capsys)
        assert "(nyc1)" not in out and "(nyc3)" not in out
        assert default == "tor1" and slug == "tor1"

    def test_utc_minus_8_user_gets_sfo2(self, capsys):
        """sfo3 is not offered for this size; sfo2 is the west-coast option."""
        slug, out, default = _run_region(ALLOWED, capsys, utc=-8)
        assert "(sfo3)" not in out and "(sfo2)" in out
        assert default == "sfo2" and slug == "sfo2"

    def test_row2_all_allowed_matches_static_list(self, capsys):
        from dango.platform.cloud.provisioning import list_regions

        every = {r.slug for r in list_regions()}
        slug, out, _ = _run_region(every, capsys)
        assert all(f"({r.slug})" in out for r in list_regions())
        assert slug == "nyc1"

    def test_row3_unverified_falls_back_with_warning(self, capsys):
        slug, out, _ = _run_region(None, capsys)
        assert "Could not verify size availability" in out
        assert "(nyc1)" in out and slug == "nyc1"

    def test_row6_unknown_api_region_not_offered(self, capsys):
        _, out, _ = _run_region(ALLOWED | {"zzz9"}, capsys)
        assert "zzz9" not in out


def _drive_wizard(
    list_sizes: Any, slugs: list[str], size_choice: str = "1", utc: int = -5
) -> tuple[Any, str, MagicMock, MagicMock, list[str]]:
    """Run run_wizard end to end with only the HTTP-level list_sizes faked.

    Returns (config, plain output, size-step spy, region-step spy).
    """
    queue = iter(slugs)

    def fake_prompt(text: str, default: str | None = None, **kw: Any) -> str:
        if "Size number" in text:
            return size_choice
        if "Enter DO size slug" in text:
            return next(queue)
        return str(default)

    real_size, real_region = deploy_wizard._step_size, deploy_wizard._step_region
    size_spy = MagicMock(side_effect=real_size)
    region_spy = MagicMock(side_effect=real_region)
    order: list[str] = []
    size_spy.side_effect = lambda *a, **k: (order.append("size"), real_size(*a, **k))[1]
    region_spy.side_effect = lambda *a, **k: (order.append("region"), real_region(*a, **k))[1]

    from rich.console import Console

    console = Console(record=True, width=200)
    with (
        patch.object(deploy_wizard, "console", console),
        patch.object(deploy_wizard, "_step_prereqs"),
        patch.object(deploy_wizard, "_step_admin", return_value=("a@b.co", "pw")),
        patch.object(deploy_wizard, "_step_sources"),
        patch.object(deploy_wizard, "_step_secrets", return_value=False),
        patch.object(deploy_wizard, "_step_oauth", return_value=True),
        patch.object(deploy_wizard, "_step_backups", return_value=(False, None, None, None)),
        patch.object(deploy_wizard, "_step_cost_summary", return_value=24),
        patch.object(deploy_wizard, "_safe_confirm", return_value=True),
        patch.object(deploy_wizard, "_step_size", size_spy),
        patch.object(deploy_wizard, "_step_region", region_spy),
        patch.object(deploy_wizard.click, "prompt", fake_prompt),
        patch.object(DigitalOceanClient, "list_sizes", list_sizes, create=True),
        patch.dict("os.environ", {"DIGITALOCEAN_TOKEN": "fake"}),
        _clock(utc),
    ):
        cfg = deploy_wizard.run_wizard(Path("."))
    return cfg, _plain(console.export_text()), size_spy, region_spy, order


@pytest.mark.unit
class TestRunWizardMatrix:
    def test_row1_wizard_hides_unavailable_region(self, sizes):
        cfg, out, _, _, _ = _drive_wizard(lambda self: sizes, [])
        assert "(nyc1)" not in out and "(tor1)" in out
        assert cfg.region == "tor1" and cfg.size_slug == "s-2vcpu-4gb"

    def test_row3_api_error_falls_back_with_warning(self):
        def boom(self: Any) -> Any:
            raise CloudError("network down")

        cfg, out, _, _, _ = _drive_wizard(boom, [])
        assert "Could not verify size availability" in out
        assert "(nyc1)" in out and cfg.region == "nyc1"

    def test_row6_unknown_api_region_not_offered(self, sizes):
        sizes[0]["regions"] = [*sizes[0]["regions"], "zzz9"]
        _, out, _, _, _ = _drive_wizard(lambda self: sizes, [])
        assert "zzz9" not in out and "(tor1)" in out

    def test_row7_unknown_custom_slug_reprompts(self, sizes):
        cfg, out, _, _, _ = _drive_wizard(
            lambda self: sizes, ["s-99vcpu-1tb", "s-2vcpu-4gb"], size_choice="3"
        )
        assert "not offered by DigitalOcean" in out
        assert cfg.size_slug == "s-2vcpu-4gb"
        assert "(nyc1)" not in out

    def test_custom_slug_filters_regions_for_that_size(self, sizes):
        custom = {"slug": "s-8vcpu-16gb", "regions": CUSTOM_REGIONS, "available": True}
        cfg, out, size_spy, region_spy, order = _drive_wizard(
            lambda self: [*sizes, custom], ["s-8vcpu-16gb"], size_choice="3"
        )
        assert order == ["size", "region"]
        assert size_spy.call_args.args[0] == [*sizes, custom]
        assert region_spy.call_args.args[0] == set(CUSTOM_REGIONS)
        assert cfg.size_slug == "s-8vcpu-16gb"
        assert cfg.region == "ams3"  # nearest allowed for that size, not the default's
        assert "(tor1)" not in out and "(sgp1)" in out


@pytest.mark.unit
class TestWizardCustomSizeDirect:
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
    def _run(self, project_root: Path, region: str, client: MagicMock, capsys: Any) -> None:
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
        self.out = _plain(capsys.readouterr().out)

    def test_row4_unavailable_creates_nothing(self, tmp_path, sizes, capsys):
        client = MagicMock()
        client.list_sizes.return_value = sizes
        self._run(tmp_path, "nyc1", client, capsys)
        client.upload_ssh_key.assert_not_called()
        client.create_droplet.assert_not_called()
        self.prov.assert_not_called()
        self.fw.assert_not_called()
        self.ssh.generate_key_pair.assert_not_called()
        msg = str(self.exc.value)
        assert "nyc1" in msg and all(r in msg for r in ALLOWED)
        assert "--region" in msg and "--size" in msg

    def test_row5_available_verified_then_proceeds_in_order(self, tmp_path, sizes, capsys):
        client = MagicMock()
        client.list_sizes.return_value = sizes
        client.upload_ssh_key.return_value = {"id": 1}
        self._run(tmp_path, "sgp1", client, capsys)
        client.list_sizes.assert_called_once()
        assert "could not verify" not in self.out.lower()
        names = [c[0] for c in client.mock_calls]
        assert names.index("list_sizes") < names.index("upload_ssh_key")
        self.prov.assert_called_once()
        assert self.prov.call_args.kwargs["region"] == "sgp1"

    def test_row3_check_failure_warns_and_continues(self, tmp_path, capsys):
        client = MagicMock()
        client.list_sizes.side_effect = CloudError("network down")
        client.upload_ssh_key.return_value = {"id": 1}
        self._run(tmp_path, "nyc1", client, capsys)
        client.upload_ssh_key.assert_called_once()
        self.prov.assert_called_once()
        assert "could not verify size availability" in self.out.lower()
