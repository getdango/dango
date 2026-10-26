"""tests/unit/test_metabase_pin_round2.py

Review round 2 of the Metabase build guard: a custom version is refused before a BYOS setup as
before a DigitalOcean droplet, an unusable local recipe fails closed, no recovery text promises a
re-run that cannot work, and the rebuild-pending flag is written without following links.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.cli.commands import deploy_provision as dp
from dango.cli.commands.deploy_wizard import BYOSConfig
from dango.exceptions import CloudProvisioningError
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud import compose_steps as cs
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


def _custom(root: Path, version: str = "v0.99.1") -> None:
    (root / "Dockerfile.metabase").write_text(f"FROM x\nENV MB_VERSION={version}\n")


def _byos() -> BYOSConfig:
    return BYOSConfig("203.0.113.5", "root", "~/.ssh/k", None, "a@b.co", "pw-long-enough-1", True)


def _do_config() -> Any:
    config = MagicMock()
    config.region, config.size_slug, config.domain = "nyc1", "s-2vcpu-4gb", None
    config.admin_email, config.admin_password = "admin@test.com", "strongpassword123"
    config.enable_backups = False
    return config


# --- M1: a custom version is refused before any BYOS server change -----------------------------


def test_byos_refuses_a_custom_version_before_any_server_change(tmp_path: Path) -> None:
    _custom(tmp_path)
    with (
        patch("dango.platform.cloud.ssh.SSHManager") as ssh_cls,
        patch("dango.platform.cloud.server_setup.setup_server") as setup,
        pytest.raises(CloudProvisioningError, match="v0.99.1") as err,
    ):
        dp.run_byos_setup(tmp_path, _byos())
    setup.assert_not_called()
    ssh_cls.return_value.connect.assert_not_called()
    text = " ".join(str(err.value).split())
    assert "default" in text and "pin-metabase VERSION SHA256" in text
    assert "dango remote push" in text and "BYOS" in text


def test_digitalocean_message_names_the_push_command(tmp_path: Path) -> None:
    _custom(tmp_path)
    with pytest.raises(CloudProvisioningError) as err:
        dp.run_provisioning(tmp_path, _do_config())
    text = " ".join(str(err.value).split())
    assert "dango remote push" in text and "pin-metabase VERSION SHA256" in text


def test_no_recovery_text_promises_a_plain_deploy_rerun() -> None:
    assert not hasattr(cm, "PIN_RECOVERY_DEPLOY")
    srv = FakeServer(marker=True, in_group=False, fail={"P2": 68})
    with pytest.raises(cm.MetabasePinRequired) as err:
        dp._prebuild_images(srv, [], own_marker=True)
    text = " ".join(str(err.value).split())
    assert "then re-run `dango deploy`" not in text and "cloud.yml" in text
    srv = FakeServer(marker=True, in_group=False, fail={"build": 66})
    with pytest.raises(CloudProvisioningError) as err2:
        cm.prebuild_images(srv, [], own_marker=True)
    assert "cloud.yml" in str(err2.value)


# --- L3: an unusable local recipe is refused, nothing is created -------------------------------


@pytest.mark.parametrize("kind", ["conflicting", "no_version", "symlink"])
@pytest.mark.parametrize("provider", ["digitalocean", "byos"])
def test_unusable_recipe_fails_closed(tmp_path: Path, kind: str, provider: str) -> None:
    recipe = tmp_path / "Dockerfile.metabase"
    if kind == "conflicting":
        recipe.write_text("ENV MB_VERSION=v0.1.0\nENV MB_VERSION=v0.2.0\n")
    elif kind == "no_version":
        recipe.write_text("FROM x\n")
    else:
        target = tmp_path / "real"
        target.write_text("ENV MB_VERSION=v0.59.1\n")
        recipe.symlink_to(target)
    with (
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient") as client,
        patch("dango.platform.cloud.ssh.SSHManager") as ssh_cls,
        patch("dango.platform.cloud.server_setup.setup_server") as setup,
        patch("dango.platform.cloud.size_availability.check_size_in_region", return_value=True),
        patch("dango.platform.cloud.provisioning.provision_droplet", side_effect=OSError("x")),
        patch("dango.cli.commands.deploy_provision.time.sleep"),
        pytest.raises(CloudProvisioningError, match="Dockerfile.metabase"),
    ):
        if provider == "byos":
            dp.run_byos_setup(tmp_path, _byos())
        else:
            dp.run_provisioning(tmp_path, _do_config())
    client.assert_not_called()
    setup.assert_not_called()
    ssh_cls.assert_not_called()


# --- L1: the rebuild-pending flag is written, read and cleared without following links --------


def _portable(script: str, tmp: Path) -> str:
    """The production script, pointed at a scratch dir owned by the current user."""
    me = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
    script = script.replace("-user root", f"-user {me}")
    return script.replace("-perm /022", "-perm +022") if sys.platform == "darwin" else script


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    srv = tmp_path / "srv"
    compose = srv / "compose"
    compose.mkdir(parents=True)
    srv.chmod(0o755)
    compose.chmod(0o755)
    return srv, compose, compose / "metabase-rebuild-pending"


def _run(script: str, tmp: Path, umask: str = "022") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f"umask {umask}; " + _portable(script, tmp)],
        capture_output=True,
        text=True,
        check=False,
    )


def _scripts(layout: tuple[Path, Path, Path]) -> tuple[str, str, str]:
    srv, compose, flag = layout
    args = (str(flag), str(srv), str(compose))
    return cs.mark_script(*args), cs.read_script(*args), cs.clear_script(*args)


def test_flag_is_created_0600_regardless_of_umask(layout: tuple[Path, Path, Path]) -> None:
    mark, read, clear = _scripts(layout)
    flag = layout[2]
    assert _run(mark, flag.parent, umask="000").returncode == 0
    assert flag.is_file() and not flag.is_symlink() and (flag.stat().st_mode & 0o777) == 0o600
    assert "PENDING" in _run(read, flag.parent).stdout
    assert _run(clear, flag.parent).returncode == 0
    assert not flag.exists() and "PENDING" not in _run(read, flag.parent).stdout


def test_planted_symlink_is_not_followed_by_any_step(
    layout: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    mark, read, clear = _scripts(layout)
    flag = layout[2]
    victim = tmp_path / "victim"
    victim.write_text("keep")
    flag.symlink_to(victim)
    assert _run(mark, flag.parent).returncode != 0
    assert victim.read_text() == "keep" and flag.is_symlink()
    assert "PENDING" not in _run(read, flag.parent).stdout
    assert _run(clear, flag.parent).returncode == 0
    assert victim.read_text() == "keep"


def test_untrusted_directory_is_refused(layout: tuple[Path, Path, Path]) -> None:
    mark, read, _ = _scripts(layout)
    compose = layout[1]
    compose.chmod(0o775)
    assert _run(mark, compose).returncode != 0 and not layout[2].exists()
    compose.chmod(0o755)
    assert _run(mark, compose).returncode == 0
    compose.chmod(0o775)
    assert "PENDING" not in _run(read, compose).stdout


def test_compose_directory_symlink_is_refused(tmp_path: Path) -> None:
    srv = tmp_path / "srv"
    srv.mkdir()
    srv.chmod(0o755)
    outside = tmp_path / "outside"
    outside.mkdir()
    outside.chmod(0o755)
    (srv / "compose").symlink_to(outside)
    flag = srv / "compose" / "metabase-rebuild-pending"
    mark = cs.mark_script(str(flag), str(srv), str(srv / "compose"))
    assert _run(mark, tmp_path).returncode != 0
    assert list(outside.iterdir()) == []


def test_ssh_calls_use_the_safe_scripts() -> None:
    srv = FakeServer(marker=True, in_group=False)
    cs.mark_rebuild_pending(srv)
    assert srv.rebuild_pending and not any(c.startswith("touch") for c in srv.commands)
    assert cs.rebuild_pending(srv)
    cs.clear_rebuild_pending(srv)
    assert not srv.rebuild_pending
    assert os.name == "posix"
