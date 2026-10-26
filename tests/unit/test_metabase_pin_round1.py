"""tests/unit/test_metabase_pin_round1.py

Review round 1 of the Metabase build guard: a failed fresh deploy does not enter the docker-group
fallback, each caller gets a recovery text that works, a custom version is refused before a
DigitalOcean droplet exists, and the strict rebuild fails closed and is retried.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_provision as dp
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud import compose_steps as cs
from dango.platform.cloud import server_setup
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    backup = MagicMock()
    backup.archive_path = "/srv/dango/backups/deploy/b.tar.gz"
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup),
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield


# --- M1: a failed fresh deploy is not left in the docker-group fallback state ------------------


@pytest.mark.parametrize("failing", [("build", 66), ("build", 75), ("P2", 65), ("P3", 65)])
def test_failed_fresh_deploy_does_not_enter_the_group_fallback(failing: tuple[str, int]) -> None:
    srv = FakeServer(marker=True, in_group=False, fail=dict([failing]))
    with pytest.raises(CloudProvisioningError):
        cm.prebuild_images(srv, [], own_marker=True)
    assert "FB" not in srv.events and "usermod" not in srv.events
    assert srv.marker and not srv.in_group
    assert not any("docker compose" in c and "# step:" not in c for c in srv.commands)


# --- M2: the recovery text of each caller works ------------------------------------------------


def _refused() -> FakeServer:
    return FakeServer(marker=True, in_group=False, fail={"P2": 68})


def test_byos_deploy_pin_refusal_says_to_pin_then_rerun_deploy() -> None:
    with pytest.raises(cm.MetabasePinRequired) as err:
        dp._prebuild_images(_refused(), [], own_marker=True)
    text = str(err.value)
    assert "pin-metabase VERSION SHA256" in text and "dango deploy" in text
    assert "remote upgrade" not in text and "transient" not in text


def test_upgrade_pin_refusal_still_says_to_rerun_remote_upgrade() -> None:
    srv = FakeServer(version="1.0.13", fail={"P2": 68})
    with pytest.raises(cm.MetabasePinRequired, match="remote upgrade") as err:
        upgrade_dango(srv, version="1.0.14")
    assert "dango deploy" not in str(err.value)


def test_digitalocean_prebuild_refusal_does_not_promise_a_pin_on_the_server() -> None:
    with pytest.raises(cm.MetabasePinRequired) as err:
        dp._prebuild_images(_refused(), [], own_marker=True, provider="digitalocean")
    text = str(err.value)
    assert "default" in text and "remote upgrade" not in text


def _custom_project(root: Path, version: str) -> None:
    (root / "Dockerfile.metabase").write_text(f"FROM x\nENV MB_VERSION={version}\n")


def test_custom_metabase_version_reader(tmp_path: Path) -> None:
    from dango.platform.cloud.metabase_pin import DEFAULT_MB_VERSION

    assert cs.custom_metabase_version(tmp_path) is None  # no file: the packaged default
    _custom_project(tmp_path, DEFAULT_MB_VERSION)
    assert cs.custom_metabase_version(tmp_path) is None
    _custom_project(tmp_path, "v0.99.1")
    assert cs.custom_metabase_version(tmp_path) == "v0.99.1"


def _wizard() -> Any:
    config = MagicMock()
    config.region, config.size_slug, config.domain = "nyc1", "s-2vcpu-4gb", None
    config.admin_email, config.admin_password = "admin@test.com", "strongpassword123"
    config.enable_backups = False
    return config


def test_digitalocean_refuses_a_custom_version_before_creating_anything(tmp_path: Path) -> None:
    _custom_project(tmp_path, "v0.99.1")
    client = MagicMock()
    with (
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
        patch("dango.platform.cloud.provisioning.provision_droplet") as droplet,
        pytest.raises(CloudProvisioningError, match="v0.99.1") as err,
    ):
        dp.run_provisioning(tmp_path, _wizard())
    assert "default" in str(err.value) and "pin" in str(err.value)
    droplet.assert_not_called()
    client.upload_ssh_key.assert_not_called()


def test_digitalocean_pin_refusal_never_prints_the_transient_retry_text(
    tmp_path: Path, capsys: Any
) -> None:
    client = MagicMock()
    ssh = MagicMock()
    ssh.generate_key_pair.return_value = "ssh-ed25519 AAAA"
    refusal = cm.MetabasePinRequired("pin needed")
    droplet = {"id": 2, "networks": {"v4": [{"type": "public", "ip_address": "1.2.3.4"}]}}
    with (
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=client),
        patch("dango.platform.cloud.ssh.SSHManager", return_value=ssh),
        patch("dango.platform.cloud.provisioning.provision_droplet", return_value=droplet),
        patch("dango.platform.cloud.firewall.create_default_firewall", return_value={"id": "f"}),
        patch("dango.platform.cloud.server_setup.setup_server", side_effect=refusal),
        patch("dango.platform.cloud.file_sync.sync_project_files"),
        patch("dango.cli.commands.deploy_provision.time.sleep"),
        pytest.raises(CloudProvisioningError),
    ):
        dp.run_provisioning(tmp_path, _wizard())
    out = " ".join(capsys.readouterr().out.split())
    assert "transient" not in out


# --- Low: setup re-run disarms the guard on a pin refusal --------------------------------------


def test_setup_rerun_disarms_the_guard_on_a_pin_refusal() -> None:
    from dango.platform.cloud.upgrade_guard import UpgradeGuard

    srv = FakeServer(project=True, in_group=True, fail={"P2": 68})
    with (
        patch.object(UpgradeGuard, "disarm", autospec=True) as disarm,
        pytest.raises(cm.MetabasePinRequired),
    ):
        server_setup._setup_compose_helper(srv, server_setup.SetupResult(), None)
    assert "web-stop" not in srv.events
    disarm.assert_called_once()


# --- Low: strict mode fails closed on an unreadable version ------------------------------------


def test_unreadable_version_rebuild_is_strict_and_fails_closed() -> None:
    srv = FakeServer(marker=False, in_group=True)
    with (
        patch(
            "dango.platform.cloud.backup.get_remote_compose_project_name",
            return_value="dango-abc",
        ),
        pytest.raises(CloudProvisioningError),
    ):
        cs.upgrade_rebuild(srv, [], "dango-abc", None)
    assert "strict-refused" in srv.events and "legacy:up-build" not in srv.events


def test_known_pre_helper_version_keeps_the_legacy_rebuild() -> None:
    srv = FakeServer(marker=False, in_group=True)
    with patch(
        "dango.platform.cloud.backup.get_remote_compose_project_name", return_value="dango-abc"
    ):
        assert cs.upgrade_rebuild(srv, [], "dango-abc", "1.0.13") is True
    assert "legacy:up-build" in srv.events and not any("exit 68" in c for c in srv.commands)


# --- Low: a failed strict rebuild is retried by re-running the upgrade -------------------------


def test_failed_rebuild_is_retried_when_the_upgrade_is_rerun_at_the_same_version() -> None:
    srv = FakeServer(version="1.0.13", fail={"up-build": 1})
    with pytest.raises(CloudProvisioningError, match="rebuild"):
        upgrade_dango(srv, version="1.0.14")
    assert srv.marker and srv.rebuild_pending
    del srv.fail["up-build"]
    upgrade_dango(srv, version="1.0.14")
    assert srv.events.count("helper:up-build") == 2
    assert not srv.rebuild_pending
    upgrade_dango(srv, version="1.0.14")  # settled again: nothing is rebuilt a third time
    assert srv.events.count("helper:up-build") == 2


def test_rerun_without_a_failed_rebuild_touches_nothing() -> None:
    srv = FakeServer(version="1.0.14", marker=True, in_group=False)
    upgrade_dango(srv, version="1.0.14")
    assert "helper:up-build" not in srv.events and "web-stop" not in srv.events
