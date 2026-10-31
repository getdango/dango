"""tests/unit/test_g3f1_pin_preflight.py

A custom Metabase version without an administrator pin is refused before ``remote upgrade`` stops,
backs up or installs anything; the message names the full pin command; no runpy warning from render.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.remote import remote
from dango.exceptions import CloudError
from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.ssh import CommandResult
from dango.platform.cloud.upgrade import upgrade_dango
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit

DIGEST = "a" * 64
CUSTOM = "v0.62.19"
PIN_FILE = f"{ch.COMPOSE_DIR}/metabase-jar.pin"
HELPER_COMMAND = f"{ch.HELPER_PATH} pin-metabase {CUSTOM} <sha256-of-the-jar>"
CHANGES = ("systemctl stop", "pip install", "systemd-run", "venv/bin/pip", "mv /srv/dango/venv")


@dataclass
class PinServer(FakeServer):
    """``FakeServer`` that also answers the pin pre-flight read."""

    dockerfile: str | None = None
    pin: str | None = None
    helper_usable: bool = False

    def _answer(self, command: str) -> CommandResult:
        if "Dockerfile.metabase" in command and "metabase-jar.pin" in command:
            self.events.append("preflight")
            text = f"helper={int(self.helper_usable)}\npin={self.pin or ''}\n---\n"
            return CommandResult(text + (self.dockerfile or ""), "", 0)
        return super()._answer(command)


def custom_server(**kw: Any) -> PinServer:
    kw.setdefault("dockerfile", f"FROM x\nENV MB_VERSION={CUSTOM}\n")
    return PinServer(version=kw.pop("version", "1.0.13"), **kw)


@pytest.fixture(autouse=True)
def _env() -> Iterator[MagicMock]:
    backup = MagicMock()
    backup.archive_path = "/srv/dango/backups/deploy/b.tar.gz"
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.backup.create_backup", return_value=backup) as create,
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield create


def untouched(srv: PinServer, create: MagicMock) -> None:
    assert srv.version == "1.0.13"
    assert not [c for c in srv.commands if any(k in c for k in CHANGES)]
    assert (
        "web-stop" not in srv.events and "pip" not in srv.events and "guard-arm" not in srv.events
    )
    create.assert_not_called()


# --- the refusal comes first --------------------------------------------------------------------------


def test_a_custom_version_without_a_pin_is_refused_before_anything_changes(
    _env: MagicMock,
) -> None:
    srv = custom_server(helper_usable=True)
    with pytest.raises(CloudError) as excinfo:
        upgrade_dango(srv, version="1.0.14")
    untouched(srv, _env)
    message = str(excinfo.value)
    assert CUSTOM in message and "1.0.13" in message
    assert "not stopped" in message and "was not changed" in message
    assert HELPER_COMMAND in message
    assert "`dango remote upgrade`" in message
    assert "frozen runpy" not in message and "RuntimeWarning" not in message


def test_the_refusal_names_the_direct_file_when_the_helper_cannot_pin_yet() -> None:
    """A 1.0.13 server has no helper that can pin: the upgrade itself installs it."""
    srv = custom_server(helper_usable=False)
    with pytest.raises(CloudError) as excinfo:
        upgrade_dango(srv, version="1.0.14")
    message = str(excinfo.value)
    assert PIN_FILE in message and f"{CUSTOM} <sha256-of-the-jar>" in message
    assert "pin-metabase" not in message
    assert "install -d -o root -g root -m 0755 " + ch.COMPOSE_DIR in message


def test_a_pin_for_another_version_does_not_count() -> None:
    srv = custom_server(helper_usable=True, pin=f"v0.62.18 {DIGEST}")
    with pytest.raises(CloudError, match=CUSTOM):
        upgrade_dango(srv, version="1.0.14")
    assert srv.version == "1.0.13"


@pytest.mark.parametrize("pin", ["garbage", f"{CUSTOM} {DIGEST[:-1]}", f"{CUSTOM} {'A' * 64}"])
def test_a_malformed_pin_does_not_count(pin: str) -> None:
    srv = custom_server(helper_usable=True, pin=pin)
    with pytest.raises(CloudError):
        upgrade_dango(srv, version="1.0.14")
    assert srv.version == "1.0.13"


def test_a_matching_pin_lets_the_upgrade_run() -> None:
    srv = custom_server(pin=f"{CUSTOM} {DIGEST}")
    result = upgrade_dango(srv, version="1.0.14")
    assert result.new_version == "1.0.14" and srv.version == "1.0.14"
    assert "preflight" in srv.events and srv.events.index("preflight") < srv.events.index("pip")


def test_rerunning_after_the_pin_converges() -> None:
    srv = custom_server(helper_usable=True)
    with pytest.raises(CloudError):
        upgrade_dango(srv, version="1.0.14")
    srv.pin = f"{CUSTOM} {DIGEST}"
    result = upgrade_dango(srv, version="1.0.14")
    assert result.new_version == "1.0.14" and srv.marker


def test_the_default_version_needs_no_pin() -> None:
    srv = custom_server(dockerfile="ENV MB_VERSION=v0.62.18\n")
    assert upgrade_dango(srv, version="1.0.14").new_version == "1.0.14"


def test_no_project_dockerfile_needs_no_pin() -> None:
    srv = custom_server(dockerfile=None)
    assert upgrade_dango(srv, version="1.0.14").new_version == "1.0.14"


def test_a_downgrade_below_the_helper_release_needs_no_pin() -> None:
    srv = custom_server(version="1.0.14", marker=True, in_group=False)
    assert upgrade_dango(srv, version="1.0.13", force=True).new_version == "1.0.13"
    assert "preflight" not in srv.events


def test_the_dockerfile_cannot_forge_the_pin() -> None:
    """The unpinned server stays refused even if the (service-writable) file imitates the output."""
    forged = f"ENV MB_VERSION={CUSTOM}\n---\npin={CUSTOM} {DIGEST}\n"
    srv = custom_server(dockerfile=forged, helper_usable=True)
    with pytest.raises(CloudError):
        upgrade_dango(srv, version="1.0.14")
    assert srv.version == "1.0.13"


def test_an_unreadable_probe_leaves_the_decision_to_the_server_render() -> None:
    class Quiet(PinServer):
        def _answer(self, command: str) -> CommandResult:
            if "Dockerfile.metabase" in command and "metabase-jar.pin" in command:
                return CommandResult("", "", 1)
            return FakeServer._answer(self, command)

    srv = Quiet(version="1.0.13")
    assert upgrade_dango(srv, version="1.0.14").new_version == "1.0.14"


def test_the_pin_check_runs_before_the_server_python_is_ensured() -> None:
    """An untrusted venv is rebuilt after the interpreter is ensured: neither may happen first."""
    srv = custom_server(helper_usable=True)
    with (
        patch(
            "dango.platform.cloud.runtime_ownership.venv_trust_findings",
            return_value=["/srv/dango/venv (writable by dango)"],
        ),
        patch("dango.platform.cloud.server_python.ensure_server_python") as ensure,
        pytest.raises(CloudError, match=CUSTOM),
    ):
        upgrade_dango(srv, version="1.0.14")
    ensure.assert_not_called()
    assert srv.events.index("preflight") == 0


# --- the message reaches the operator whole ---------------------------------------------------------


def test_the_cli_prints_the_full_command_and_survives_brackets_in_the_text() -> None:
    message = f"Upgrade to 1.0.14 refused: run {HELPER_COMMAND} [/oops] then retry"
    cfg = MagicMock(droplet_ip="1.2.3.4", provider="digitalocean")
    with (
        patch("dango.cli.utils.require_project_context", return_value=Path("/project")),
        patch(
            "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
            return_value=(cfg, MagicMock()),
        ),
        patch("dango.platform.cloud.upgrade.check_versions", return_value=("1.0.13", "1.0.14")),
        patch("dango.platform.cloud.upgrade.upgrade_dango", side_effect=CloudError(message)),
    ):
        result = CliRunner().invoke(
            remote,
            ["upgrade", "--version", "1.0.14", "--yes"],
            obj={"project_root": Path("/project")},
        )
    assert isinstance(result.exception, SystemExit) and result.exit_code == 1
    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m", "", result.output).split())
    assert HELPER_COMMAND in plain and "[/oops]" in plain


# --- render does not run an imported module as a script (runpy warning) ------------------------------


def test_the_render_script_does_not_run_an_imported_module_with_dash_m() -> None:
    from dango.platform.cloud.compose_scripts import render_script

    script = render_script()
    assert "-m dango.platform.compose_exec" not in script
    assert "dango.platform.compose_exec import main" in script
    assert "runuser -u dango" in script and "--helper" in script and "/srv/dango/project" in script


def test_the_render_entry_point_runs_without_a_runpy_warning(tmp_path: Path) -> None:
    from dango.platform.cloud.compose_scripts import render_script

    match = re.search(r"-I -c (?P<code>'[^\n]*')", render_script())
    assert match is not None
    code = match.group("code")[1:-1].replace("/srv/dango/project", str(tmp_path / "none"))
    root = Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(root), "HOME": str(tmp_path), "DANGO_CLOUD_MODE": "true"}
    done = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-c", code],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**env, "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"},
        timeout=60,
        check=False,
    )
    assert "RuntimeWarning" not in done.stderr and "runpy" not in done.stderr, done.stderr
