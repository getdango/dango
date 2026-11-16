"""tests/unit/test_serve_loopback_bind.py

On servers dango-web listens on 127.0.0.1 only (Caddy proxies to it); no firewall rule opens 8800.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

_STARTUP = "dango.platform.common.startup"
_SERVE = "dango.cli.commands.serve"


def _run_serve(tmp_path: Path, args: list[str]) -> tuple[MagicMock, MagicMock]:
    """Run `dango serve` with every startup step faked; return (uvicorn.run, _check_port)."""
    from dango.cli.commands.serve import serve

    config = MagicMock()
    config.project.name = "p"
    config.project.organization = None
    config.platform.port = 8800
    config.platform.workers = None
    loader = MagicMock()
    loader.load_config.return_value = config
    with (
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
        patch("dango.config.ConfigLoader", return_value=loader),
        patch(f"{_STARTUP}.run_pending_migrations", return_value={}),
        patch(f"{_STARTUP}.ensure_dbt_schemas"),
        patch(f"{_STARTUP}.ensure_duckdb_driver"),
        patch(f"{_STARTUP}.start_docker_services"),
        patch(f"{_STARTUP}.setup_metabase_if_needed"),
        patch(f"{_STARTUP}.import_dashboards"),
        patch(f"{_SERVE}._check_port") as check_port,
        patch("uvicorn.run") as uvicorn_run,
    ):
        result = CliRunner().invoke(serve, args, obj={})
    assert result.exit_code == 0, result.output
    return uvicorn_run, check_port


@pytest.mark.unit
class TestServerBindsLoopback:
    @pytest.mark.parametrize("workers", [None, 4])
    def test_systemd_unit_binds_loopback(self, workers):
        from dango.platform.cloud._server_templates import build_systemd_unit

        exec_start = next(
            line
            for line in build_systemd_unit(workers).splitlines()
            if line.startswith("ExecStart=")
        )
        assert "--host 127.0.0.1" in exec_start

    def test_serve_in_cloud_mode_defaults_to_loopback(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        uvicorn_run, check_port = _run_serve(tmp_path, [])
        assert uvicorn_run.call_args.kwargs["host"] == "127.0.0.1"
        assert "127.0.0.1" in [*check_port.call_args.args, *check_port.call_args.kwargs.values()]

    def test_serve_outside_cloud_mode_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        uvicorn_run, _check = _run_serve(tmp_path, [])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"

    def test_explicit_host_wins_in_cloud_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        uvicorn_run, _check = _run_serve(tmp_path, ["--host", "0.0.0.0"])
        assert uvicorn_run.call_args.kwargs["host"] == "0.0.0.0"


@pytest.mark.unit
class TestFirewallsNeverOpenWebAppPort:
    def test_digitalocean_default_rules(self):
        from dango.platform.cloud.firewall import DEFAULT_INBOUND_RULES

        assert {rule["ports"] for rule in DEFAULT_INBOUND_RULES} == {"22", "80", "443"}

    def test_byos_ufw_rules(self):
        from dango.platform.cloud.server_setup import SetupResult, _setup_ufw

        ssh = MagicMock()
        inactive = MagicMock(success=False, exit_code=1, stdout="", stderr="")
        ok = MagicMock(success=True, exit_code=0, stdout="", stderr="")
        ssh.exec_command.side_effect = [inactive, ok]
        _setup_ufw(ssh, SetupResult(), None)
        commands = " ".join(str(c) for c in ssh.exec_command.call_args_list)
        assert "ufw allow 80/tcp" in commands
        assert "8800" not in commands
