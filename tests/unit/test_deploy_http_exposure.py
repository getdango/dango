"""tests/unit/test_deploy_http_exposure.py

Deploys without a domain must be IP-restricted or explicitly acknowledged (CLI, wizard, BYOS, summary).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

_PASSWORD = "Str0ng-and-l0ng-passw0rd!"


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    dango_dir = tmp_path / ".dango"
    dango_dir.mkdir()
    (dango_dir / "sources.yml").write_text("sources: []")
    (dango_dir / "project.yml").write_text("project:\n  name: test\n")
    (dango_dir / "cloud_key").write_text("fake-key")
    return tmp_path


def _provision_result(allowed_ips: list[str] | None = None) -> MagicMock:
    result = MagicMock()
    result.url = "http://198.51.100.10"
    result.droplet_ip = "198.51.100.10"
    result.server_ip = "198.51.100.10"
    result.admin_password = _PASSWORD
    result.warnings = []
    result.allowed_ips = allowed_ips or []
    return result


def _invoke_deploy(project_root: Path, args: list[str]) -> tuple[click.testing.Result, MagicMock]:
    """Run ``dango deploy`` with DO prerequisites and provisioning faked."""
    from dango.cli.commands.deploy import deploy

    run_prov = MagicMock(return_value=_provision_result())
    with (
        patch("dango.cli.utils.find_project_root", return_value=project_root),
        patch("dango.cli.commands.deploy_wizard._step_prereqs"),
        patch("dango.cli.commands.deploy_provision.run_provisioning", run_prov),
        patch("webbrowser.open"),
    ):
        result = CliRunner().invoke(deploy, args, obj={"project_root": project_root})
    return result, run_prov


_DO_ARGS = [
    "--non-interactive",
    "--region",
    "nyc1",
    "--size",
    "s-2vcpu-4gb",
    "--admin-email",
    "admin@example.com",
    "--admin-password",
    _PASSWORD,
]


# ---------------------------------------------------------------------------
# Non-interactive DigitalOcean deploy
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNonInteractiveFlags:
    def test_non_interactive_without_domain_requires_allow_ip_or_accept(self, project_root):
        result, run_prov = _invoke_deploy(project_root, _DO_ARGS)
        assert result.exit_code == 2, result.output
        assert "--allow-ip" in result.output
        assert "--accept-http-exposure" in result.output
        run_prov.assert_not_called()

    def test_non_interactive_with_allow_ip_ok(self, project_root):
        result, run_prov = _invoke_deploy(project_root, [*_DO_ARGS, "--allow-ip", "203.0.113.7"])
        assert result.exit_code == 0, result.output
        config = run_prov.call_args.args[1]
        assert config.allowed_ips == ["203.0.113.7/32"]

    def test_allow_ip_is_repeatable(self, project_root):
        result, run_prov = _invoke_deploy(
            project_root,
            [*_DO_ARGS, "--allow-ip", "203.0.113.7", "--allow-ip", "198.51.100.0/24"],
        )
        assert result.exit_code == 0, result.output
        assert run_prov.call_args.args[1].allowed_ips == ["203.0.113.7/32", "198.51.100.0/24"]

    def test_non_interactive_with_accept_flag_ok(self, project_root):
        result, run_prov = _invoke_deploy(project_root, [*_DO_ARGS, "--accept-http-exposure"])
        assert result.exit_code == 0, result.output
        config = run_prov.call_args.args[1]
        assert config.allowed_ips == []
        assert config.http_exposure_acknowledged is True

    def test_non_interactive_with_domain_needs_neither(self, project_root):
        result, run_prov = _invoke_deploy(project_root, [*_DO_ARGS, "--domain", "app.example.com"])
        assert result.exit_code == 0, result.output
        assert run_prov.call_args.args[1].domain == "app.example.com"

    def test_allow_ip_invalid_rejected(self, project_root):
        result, run_prov = _invoke_deploy(project_root, [*_DO_ARGS, "--allow-ip", "999.1.1.1"])
        assert result.exit_code == 2, result.output
        assert "999.1.1.1" in result.output
        run_prov.assert_not_called()

    def test_non_interactive_rejects_detect(self, project_root):
        result, run_prov = _invoke_deploy(project_root, [*_DO_ARGS, "--allow-ip", "detect"])
        assert result.exit_code == 2, result.output
        assert "detect" in result.output
        run_prov.assert_not_called()


# ---------------------------------------------------------------------------
# Interactive wizard
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestWizardExposureStep:
    def _run_step(self, answers: list[str]):
        from dango.cli.commands.deploy_wizard import _step_web_exposure

        with patch("dango.cli.commands.deploy_wizard.click.prompt", side_effect=answers) as p:
            return _step_web_exposure(), p

    def test_wizard_ip_restricts(self):
        (allowed, acknowledged), _ = self._run_step(["203.0.113.7"])
        assert allowed == ["203.0.113.7/32"]
        assert acknowledged is False

    def test_wizard_empty_ip_requires_confirmation(self):
        # Empty input, then anything but "yes" goes back to the IP question.
        (allowed, acknowledged), prompt = self._run_step(["", "no", "203.0.113.7"])
        assert allowed == ["203.0.113.7/32"]
        assert acknowledged is False
        assert prompt.call_count == 3

        (allowed, acknowledged), _ = self._run_step(["", "yes"])
        assert allowed == []
        assert acknowledged is True

    def test_wizard_invalid_ip_reprompts(self):
        (allowed, _ack), prompt = self._run_step(["not-an-ip", "203.0.113.0/24"])
        assert allowed == ["203.0.113.0/24"]
        assert prompt.call_count == 2

    def test_wizard_detect_is_offered_and_stored(self):
        (allowed, _ack), prompt = self._run_step(["detect"])
        assert allowed == ["detect"]
        assert "detect" in prompt.call_args_list[0].args[0]

    def test_wizard_config_defaults_keep_old_constructors_working(self):
        from dango.cli.commands.deploy_wizard import WizardConfig

        cfg = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain=None,
            admin_email="admin@example.com",
            admin_password=_PASSWORD,
            skip_oauth=True,
            enable_backups=False,
            monthly_cost=24,
        )
        assert cfg.allowed_ips == []
        assert cfg.http_exposure_acknowledged is False

    def test_run_wizard_stores_exposure_choice(self, project_root):
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
            patch.object(dw.click, "prompt", side_effect=["203.0.113.7"]),
        ):
            cfg = dw.run_wizard(project_root)
        assert cfg.domain is None
        assert cfg.allowed_ips == ["203.0.113.7/32"]


# ---------------------------------------------------------------------------
# BYOS
# ---------------------------------------------------------------------------


def _byos_args(project_root: Path, *extra: str) -> list[str]:
    return [
        "--byos",
        "--server-ip",
        "198.51.100.10",
        "--ssh-key",
        str(project_root / ".dango" / "cloud_key"),
        "--admin-email",
        "admin@example.com",
        "--admin-password",
        _PASSWORD,
        *extra,
    ]


def _invoke_byos(project_root: Path, args: list[str]):
    from dango.cli.commands.deploy import deploy

    ssh_cls = MagicMock()
    run_setup = MagicMock(return_value=_provision_result())
    with (
        patch("dango.cli.utils.find_project_root", return_value=project_root),
        patch("dango.platform.cloud.ssh.SSHManager", ssh_cls),
        patch("dango.cli.commands.deploy_provision.run_byos_setup", run_setup),
        patch("webbrowser.open"),
    ):
        result = CliRunner().invoke(deploy, args, obj={"project_root": project_root})
    return result, ssh_cls, run_setup


@pytest.mark.unit
class TestByosExposure:
    def test_byos_without_domain_requires_flag(self, project_root):
        result, ssh_cls, run_setup = _invoke_byos(project_root, _byos_args(project_root))
        assert result.exit_code == 2, result.output
        assert "--accept-http-exposure" in result.output
        ssh_cls.return_value.connect.assert_not_called()
        run_setup.assert_not_called()

    def test_byos_without_domain_requires_accept_flag(self, project_root):
        result, _ssh, run_setup = _invoke_byos(
            project_root, _byos_args(project_root, "--accept-http-exposure")
        )
        assert result.exit_code == 0, result.output
        run_setup.assert_called_once()

    def test_byos_allow_ip_is_usage_error(self, project_root):
        result, ssh_cls, run_setup = _invoke_byos(
            project_root, _byos_args(project_root, "--allow-ip", "203.0.113.7")
        )
        assert result.exit_code == 2, result.output
        assert "DigitalOcean firewall" in result.output
        ssh_cls.return_value.connect.assert_not_called()
        run_setup.assert_not_called()

    def test_byos_with_domain_needs_no_flag(self, project_root):
        result, _ssh, run_setup = _invoke_byos(
            project_root, _byos_args(project_root, "--domain", "app.example.com")
        )
        assert result.exit_code == 0, result.output
        run_setup.assert_called_once()

    def _run_byos_wizard(self, project_root: Path, answers: list[str]):
        from dango.cli.commands import deploy_wizard as dw

        with (
            patch.object(dw, "_step_byos_server", return_value=("198.51.100.10", "root", "k")),
            patch.object(dw, "_validate_ssh_connectivity"),
            patch.object(dw, "_step_admin", return_value=("admin@example.com", _PASSWORD)),
            patch.object(dw, "_step_sources", return_value=[]),
            patch.object(dw, "_step_secrets", return_value=True),
            patch.object(dw, "_step_oauth", return_value=True),
            patch.object(dw, "_safe_confirm", return_value=True),
            patch.object(dw.click, "prompt", side_effect=answers) as prompt,
        ):
            return dw.run_byos_wizard(project_root), prompt

    def test_byos_wizard_requires_typed_yes_and_offers_no_ip_prompt(self, project_root):
        cfg, prompt = self._run_byos_wizard(project_root, ["yes"])
        assert cfg.http_exposure_acknowledged is True
        assert prompt.call_count == 1
        assert "IPv4" not in prompt.call_args.args[0]

    def test_byos_wizard_refuses_without_yes(self, project_root):
        with pytest.raises(click.exceptions.Abort):
            self._run_byos_wizard(project_root, ["no"])


# ---------------------------------------------------------------------------
# BYOS requires root (sudo-based setup is not supported)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestByosRootUser:
    def test_wizard_rejects_non_root_user(self, project_root):
        from dango.cli.commands import deploy_wizard as dw

        ssh_cls = MagicMock()
        with (
            patch.object(
                dw.click,
                "prompt",
                side_effect=[
                    "198.51.100.10",
                    "ubuntu",
                    "1",
                    str(project_root / ".dango" / "cloud_key"),
                ],
            ),
            patch("dango.platform.cloud.ssh.SSHManager", ssh_cls),
            pytest.raises(click.exceptions.Abort),
        ):
            dw._step_byos_server(project_root)
        ssh_cls.return_value.connect.assert_not_called()

    def test_wizard_root_user_unchanged(self, project_root):
        from dango.cli.commands import deploy_wizard as dw

        key = str(project_root / ".dango" / "cloud_key")
        with patch.object(dw.click, "prompt", side_effect=["198.51.100.10", "root", "1", key]):
            assert dw._step_byos_server(project_root) == ("198.51.100.10", "root", key)

    def test_non_interactive_non_root_aborts_before_connect(self, project_root):
        result, ssh_cls, run_setup = _invoke_byos(
            project_root,
            _byos_args(project_root, "--ssh-user", "ubuntu", "--accept-http-exposure"),
        )
        assert result.exit_code != 0
        assert "root" in result.output
        ssh_cls.return_value.connect.assert_not_called()
        run_setup.assert_not_called()


# ---------------------------------------------------------------------------
# Deploy summary
# ---------------------------------------------------------------------------


def _summary(capsys, **kwargs) -> str:
    from dango.cli.commands.deploy import _print_deploy_success

    base = {
        "url": "http://198.51.100.10",
        "ip": "198.51.100.10",
        "admin_email": "admin@example.com",
        "warnings": [],
    }
    base.update(kwargs)
    with patch("webbrowser.open"):
        _print_deploy_success(**base)
    return " ".join(capsys.readouterr().out.split())


@pytest.mark.unit
class TestDeploySummary:
    def test_summary_warns_http_only(self, capsys):
        out = _summary(capsys, domain=None, allowed_ips=[])
        assert "HTTP only" in out
        assert "credentials travel unencrypted" in out
        assert "OPEN to the internet" in out
        assert "dango remote domain set" in out

    def test_summary_lists_allowed_ips(self, capsys):
        out = _summary(capsys, domain=None, allowed_ips=["203.0.113.7/32"])
        assert "HTTP only" in out
        assert "203.0.113.7/32" in out
        assert "OPEN to the internet" not in out

    def test_summary_with_domain_has_no_http_warning(self, capsys):
        out = _summary(capsys, url="https://app.example.com", domain="app.example.com")
        assert "HTTP only" not in out

    def test_summary_backup_claim_requires_spaces_keys(self, project_root, capsys):
        from dango.cli.commands.deploy import _handle_do_deploy_with_config
        from dango.cli.commands.deploy_wizard import WizardConfig

        cfg = WizardConfig(
            region="nyc1",
            size_slug="s-2vcpu-4gb",
            size_tier=None,
            domain="app.example.com",
            admin_email="admin@example.com",
            admin_password=_PASSWORD,
            skip_oauth=True,
            enable_backups=True,
            monthly_cost=29,
        )
        with (
            patch(
                "dango.cli.commands.deploy_provision.run_provisioning",
                return_value=_provision_result(),
            ),
            patch("webbrowser.open"),
        ):
            _handle_do_deploy_with_config(project_root, cfg)
        out = " ".join(capsys.readouterr().out.split())
        assert "to Spaces" not in out
        assert "dango remote backup enable" in out
