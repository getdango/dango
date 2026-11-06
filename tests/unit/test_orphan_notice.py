"""tests/unit/test_orphan_notice.py

Warn about droplets tagged `dango` that this project's cloud.yml does not name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands import orphan_notice
from dango.exceptions import CloudAPIError, CloudAuthError

_HINT = "delete in the DigitalOcean console"


class FakeClient:
    """Records calls; refuses to delete anything."""

    def __init__(
        self, droplets: list[dict[str, Any]] | None = None, error: Exception | None = None
    ):
        self.droplets = droplets or []
        self.error = error
        self.list_calls: list[str | None] = []

    def list_droplets(self, tag_name: str | None = None) -> list[dict[str, Any]]:
        self.list_calls.append(tag_name)
        if self.error:
            raise self.error
        return list(self.droplets)

    def delete_droplet(self, droplet_id: int) -> None:
        raise AssertionError("the orphan check must never delete")


def _droplet(droplet_id: int, name: str = "d", created: str = "2026-11-01T10:00:00Z") -> dict:
    return {"id": droplet_id, "name": name, "created_at": created}


def _project(tmp_path: Path, droplet_id: int | None) -> Path:
    if droplet_id is not None:
        (tmp_path / ".dango").mkdir()
        (tmp_path / ".dango" / "cloud.yml").write_text(
            f"droplet_id: {droplet_id}\ndroplet_ip: 1.2.3.4\n"
        )
    return tmp_path


def _warn(project: Path, client: Any, capsys: pytest.CaptureFixture[str]) -> str:
    orphan_notice.warn_untracked_droplets(project, client_factory=lambda: client)
    return capsys.readouterr().out


@pytest.mark.unit
class TestWarnUntrackedDroplets:
    def test_queries_the_dango_tag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeClient()
        _warn(_project(tmp_path, 1), client, capsys)
        assert client.list_calls == ["dango"]

    def test_no_droplets_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _warn(_project(tmp_path, 1), FakeClient([]), capsys).strip() == ""

    def test_only_the_current_droplet_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = _warn(_project(tmp_path, 7), FakeClient([_droplet(7)]), capsys)
        assert out.strip() == ""

    def test_one_orphan_lists_id_name_created_and_instructions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeClient([_droplet(7), _droplet(99, "leftover", "2026-10-30T08:15:00Z")])
        out = _warn(_project(tmp_path, 7), client, capsys)
        assert "99" in out and "leftover" in out and "2026-10-30T08:15:00Z" in out
        assert _HINT in out
        assert "killed hard" in out
        assert "(id 7" not in out and "name d" not in out  # the tracked droplet is not listed

    def test_two_orphans_both_listed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = FakeClient([_droplet(1, "a"), _droplet(2, "b"), _droplet(7, "mine")])
        out = _warn(_project(tmp_path, 7), client, capsys)
        assert "a" in out and "b" in out
        assert "1" in out and "2" in out
        assert "mine" not in out

    def test_no_cloud_yml_every_tagged_droplet_is_untracked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = _warn(_project(tmp_path, None), FakeClient([_droplet(5, "orph")]), capsys)
        assert "orph" in out

    def test_markup_in_a_droplet_name_is_printed_literally(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = _warn(_project(tmp_path, 7), FakeClient([_droplet(5, "[bold]x[/bold]")]), capsys)
        assert "[bold]x[/bold]" in out

    @pytest.mark.parametrize(
        "error", [CloudAPIError("boom"), RuntimeError("net"), CloudAuthError("t")]
    )
    def test_list_failure_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], error: Exception
    ) -> None:
        out = _warn(_project(tmp_path, 7), FakeClient(error=error), capsys)
        assert out.strip() == ""

    def test_client_factory_failure_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def factory() -> Any:
            raise CloudAuthError("no token")

        orphan_notice.warn_untracked_droplets(_project(tmp_path, 7), client_factory=factory)
        assert capsys.readouterr().out.strip() == ""

    def test_no_token_means_no_client_and_no_output(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DIGITALOCEAN_TOKEN", raising=False)
        with (
            patch("dango.config.cloud_credentials.get_do_token", return_value=None),
            patch("dango.platform.cloud.digitalocean.DigitalOceanClient") as cls,
        ):
            orphan_notice.warn_untracked_droplets(_project(tmp_path, 7))
        cls.assert_not_called()
        assert capsys.readouterr().out.strip() == ""

    def test_unreadable_cloud_yml_is_silent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / ".dango").mkdir()
        (tmp_path / ".dango" / "cloud.yml").write_text("{ not: [valid")
        out = _warn(tmp_path, FakeClient([_droplet(5)]), capsys)
        assert out.strip() == ""


@pytest.mark.unit
class TestWiring:
    """The check runs at the start of a DigitalOcean deploy and in `remote status`."""

    _WARN = "dango.cli.commands.orphan_notice.warn_untracked_droplets"

    def _deploy(self, tmp_path: Path, args: list[str]) -> tuple[Any, MagicMock, MagicMock]:
        from dango.cli.commands.deploy import deploy

        order = MagicMock()
        with (
            patch("dango.cli.utils.find_project_root", return_value=tmp_path),
            patch("dango.cli.commands.deploy_wizard._step_prereqs"),
            patch(self._WARN, order.warn),
            patch("dango.cli.commands.deploy_provision.run_provisioning", order.provision),
            patch("webbrowser.open"),
        ):
            result = CliRunner().invoke(deploy, args, obj={"project_root": tmp_path})
        return result, order.warn, order

    def test_non_interactive_do_deploy_warns_before_provisioning(self, tmp_path: Path) -> None:
        args = [
            "--non-interactive",
            "--region",
            "nyc1",
            "--size",
            "s-2vcpu-4gb",
            "--admin-email",
            "admin@example.com",
            "--admin-password",
            "Passw0rd!long-enough",
            "--accept-http-exposure",
        ]
        _, warn, order = self._deploy(tmp_path, args)
        assert warn.call_count == 1
        names = [c[0] for c in order.mock_calls]
        assert names.index("warn") < names.index("provision")

    def test_byos_deploy_does_not_warn(self, tmp_path: Path) -> None:
        _, warn, _ = self._deploy(tmp_path, ["--byos"])
        warn.assert_not_called()

    def test_remote_status_warns_for_digitalocean(self, tmp_path: Path) -> None:
        from dango.cli.commands.remote import remote
        from tests.unit.test_remote_management_cli import (
            _make_cloud_config,
            _make_loader,
            _make_server_status,
        )

        cfg = _make_cloud_config()
        cfg.provider = "digitalocean"
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.loader.ConfigLoader", return_value=_make_loader(cfg)),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=MagicMock()),
            patch(
                "dango.platform.cloud.server_status.collect_server_status",
                return_value=_make_server_status(),
            ),
            patch("dango.platform.cloud.server_status.check_latest_pypi_version"),
            patch("dango.cli.commands.backup_notices.backup_status_lines", return_value=[]),
            patch(self._WARN) as warn,
        ):
            result = CliRunner().invoke(remote, ["status"], obj={"project_root": tmp_path})
        assert result.exit_code == 0, result.output
        warn.assert_called_once()

    def test_remote_status_byos_does_not_warn(self, tmp_path: Path) -> None:
        from dango.cli.commands.remote import remote
        from tests.unit.test_remote_management_cli import (
            _make_cloud_config,
            _make_loader,
            _make_server_status,
        )

        cfg = _make_cloud_config()
        cfg.provider = "byos"
        with (
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.loader.ConfigLoader", return_value=_make_loader(cfg)),
            patch("dango.platform.cloud.ssh.SSHManager", return_value=MagicMock()),
            patch(
                "dango.platform.cloud.server_status.collect_server_status",
                return_value=_make_server_status(),
            ),
            patch("dango.platform.cloud.server_status.check_latest_pypi_version"),
            patch("dango.cli.commands.backup_notices.backup_status_lines", return_value=[]),
            patch(self._WARN) as warn,
        ):
            result = CliRunner().invoke(remote, ["status"], obj={"project_root": tmp_path})
        assert result.exit_code == 0, result.output
        warn.assert_not_called()
