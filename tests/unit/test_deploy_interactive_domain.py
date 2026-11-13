"""tests/unit/test_deploy_interactive_domain.py

Interactive `dango deploy --domain X` passes the domain to both wizards and skips the HTTP-exposure step.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

_PASSWORD = "Str0ng-and-l0ng-passw0rd!"


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "sources.yml").write_text("sources: []")
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    return tmp_path


def _invoke(project_root: Path, args: list[str], choice: str):
    from dango.cli.commands.deploy import deploy

    result_obj = MagicMock(url="https://app.example.com", warnings=[], allowed_ips=[])
    run_wizard = MagicMock(return_value=MagicMock(domain="app.example.com"))
    run_byos_wizard = MagicMock(return_value=MagicMock(domain="app.example.com"))
    with (
        patch("dango.cli.utils.find_project_root", return_value=project_root),
        patch("dango.cli.commands.deploy_wizard.run_wizard", run_wizard),
        patch("dango.cli.commands.deploy_wizard.run_byos_wizard", run_byos_wizard),
        patch("dango.cli.commands.deploy_provision.run_provisioning", return_value=result_obj),
        patch("dango.cli.commands.deploy_provision.run_byos_setup", return_value=result_obj),
        patch("webbrowser.open"),
    ):
        result = CliRunner().invoke(
            deploy, args, obj={"project_root": project_root}, input=f"{choice}\n"
        )
    return result, run_wizard, run_byos_wizard


@pytest.mark.unit
class TestInteractiveDomain:
    def test_cli_passes_domain_to_digitalocean_wizard(self, project_root):
        result, run_wizard, _byos = _invoke(project_root, ["--domain", "app.example.com"], "1")
        assert result.exit_code == 0, result.output
        assert run_wizard.call_args.kwargs["domain"] == "app.example.com"

    def test_cli_passes_domain_to_byos_wizard(self, project_root):
        result, _wizard, run_byos_wizard = _invoke(
            project_root, ["--domain", "app.example.com"], "2"
        )
        assert result.exit_code == 0, result.output
        assert run_byos_wizard.call_args.kwargs["domain"] == "app.example.com"

    def test_digitalocean_wizard_with_domain_skips_exposure_step(self, project_root):
        from dango.cli.commands import deploy_wizard as dw

        with (
            patch.object(dw, "_step_prereqs"),
            patch("dango.platform.cloud.size_availability.fetch_sizes", return_value=None),
            patch.object(dw, "_step_size", return_value=("s-2vcpu-4gb", None)),
            patch.object(dw, "_step_region", return_value="nyc1"),
            patch.object(dw, "_step_admin", return_value=("admin@example.com", _PASSWORD)),
            patch.object(dw, "_step_sources", return_value=[]),
            patch.object(dw, "_step_secrets", return_value=True),
            patch.object(dw, "_step_oauth", return_value=True),
            patch.object(dw, "_step_backups", return_value=(False, None, None, None)),
            patch.object(dw, "_step_cost_summary", return_value=24),
            patch.object(dw.click, "prompt") as prompt,
        ):
            cfg = dw.run_wizard(project_root, domain="app.example.com")
        prompt.assert_not_called()
        assert cfg.domain == "app.example.com"
        assert cfg.allowed_ips == []

    def test_byos_wizard_with_domain_skips_acknowledgement(self, project_root):
        from dango.cli.commands import deploy_wizard as dw

        with (
            patch.object(dw, "_step_byos_server", return_value=("198.51.100.10", "root", "k")),
            patch.object(dw, "_validate_ssh_connectivity"),
            patch.object(dw, "_step_admin", return_value=("admin@example.com", _PASSWORD)),
            patch.object(dw, "_step_sources", return_value=[]),
            patch.object(dw, "_step_secrets", return_value=True),
            patch.object(dw, "_step_oauth", return_value=True),
            patch.object(dw, "_safe_confirm", return_value=True),
            patch.object(dw.click, "prompt") as prompt,
        ):
            cfg = dw.run_byos_wizard(project_root, domain="app.example.com")
        prompt.assert_not_called()
        assert cfg.domain == "app.example.com"

    def test_allow_ip_with_domain_still_refused_interactively(self, project_root):
        result, run_wizard, _byos = _invoke(
            project_root, ["--domain", "app.example.com", "--allow-ip", "203.0.113.7"], "1"
        )
        assert result.exit_code == 2, result.output
        assert "Let's Encrypt" in result.output
        run_wizard.assert_not_called()
