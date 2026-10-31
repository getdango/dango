"""tests/unit/test_g3f1_review1.py

Review round 1 of G3F1: the direct root pin command is link-safe and pastes as one line, the
pre-flight agrees with the server's parsers, upgrade output escapes server text, and status never
picks between several installed-version directories.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.exceptions import CloudError
from dango.platform.cloud import isolation_status as iso
from dango.platform.cloud.ssh import CommandResult
from dango.platform.cloud.upgrade import UpgradeResult, upgrade_dango
from tests.factories.compose_switch_fakes import FakeServer
from tests.factories.remote_isolation_fakes import probe

pytestmark = pytest.mark.unit

DIGEST = "a" * 64
CUSTOM = "v0.62.19"
DASH = shutil.which("dash")


# --- L1: the direct root command ---------------------------------------------------------------------


@pytest.fixture
def shim(tmp_path: Path) -> Path:
    """A ``mv`` that accepts GNU's ``-T`` (the macOS one does not), so the real command text runs."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    mv = bin_dir / "mv"
    mv.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do case "$1" in --) shift; break;; -*) shift;; *) break;;'
        ' esac; done\nexec /bin/mv -f "$@"\n'
    )
    mv.chmod(0o755)
    return bin_dir


def run_direct(
    srv: Path, shim_dir: Path, digest: str = DIGEST, version: str = CUSTOM
) -> subprocess.CompletedProcess[str]:
    from dango.platform.cloud.upgrade_preflight import direct_pin_script

    assert DASH, "dash is required for this test"
    env = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": str(srv)}
    return subprocess.run(
        [DASH, "-c", direct_pin_script(str(srv)), "pin", version, digest],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


@pytest.fixture
def srv(tmp_path: Path) -> Path:
    root = (tmp_path / "srv").resolve()
    root.mkdir()
    return root


class TestDirectCommand:
    def test_it_is_one_line_a_shell_can_paste(self) -> None:
        from dango.platform.cloud.upgrade_preflight import recovery

        command = recovery(CUSTOM, helper_usable=False)
        assert "\n" not in command and command.startswith("sh -c ")
        assert f"{CUSTOM} <sha256-of-the-jar>" in command

    def test_it_writes_a_private_lowercase_record(self, srv: Path, shim: Path) -> None:
        done = run_direct(srv, shim, digest="A" * 64)
        assert done.returncode == 0, done.stderr
        pin = srv / "compose" / "metabase-jar.pin"
        assert pin.read_text() == f"{CUSTOM} {'a' * 64}\n"
        assert (pin.stat().st_mode & 0o777) == 0o600
        assert (srv / "compose").stat().st_mode & 0o022 == 0
        assert [p.name for p in (srv / "compose").iterdir()] == ["metabase-jar.pin"]

    def test_it_replaces_an_existing_record(self, srv: Path, shim: Path) -> None:
        (srv / "compose").mkdir(mode=0o755)
        (srv / "compose" / "metabase-jar.pin").write_text("old\n")
        assert run_direct(srv, shim).returncode == 0
        assert (srv / "compose" / "metabase-jar.pin").read_text().startswith(CUSTOM)

    def test_it_refuses_a_planted_link_at_the_record(self, srv: Path, shim: Path) -> None:
        victim = srv / "victim"
        victim.write_text("keep\n")
        (srv / "compose").mkdir(mode=0o755)
        (srv / "compose" / "metabase-jar.pin").symlink_to(victim)
        done = run_direct(srv, shim)
        assert done.returncode != 0 and "symlink" in done.stderr
        assert victim.read_text() == "keep\n"

    def test_it_refuses_a_planted_link_at_the_directory(self, srv: Path, shim: Path) -> None:
        elsewhere = srv / "elsewhere"
        elsewhere.mkdir(mode=0o700)
        (srv / "compose").symlink_to(elsewhere)
        done = run_direct(srv, shim)
        assert done.returncode != 0
        assert list(elsewhere.iterdir()) == [] and (elsewhere.stat().st_mode & 0o777) == 0o700

    def test_it_refuses_a_directory_others_can_write(self, srv: Path, shim: Path) -> None:
        (srv / "compose").mkdir()
        (srv / "compose").chmod(0o777)
        assert run_direct(srv, shim).returncode != 0
        assert list((srv / "compose").iterdir()) == []

    def test_it_refuses_a_link_in_the_path(self, tmp_path: Path, srv: Path, shim: Path) -> None:
        linked = tmp_path / "linked"
        linked.symlink_to(srv)
        assert run_direct(linked, shim).returncode != 0
        assert not (srv / "compose").exists()

    def test_it_refuses_a_record_path_that_is_not_a_file(self, srv: Path, shim: Path) -> None:
        (srv / "compose" / "metabase-jar.pin").mkdir(parents=True)
        assert run_direct(srv, shim).returncode != 0

    @pytest.mark.parametrize("digest", ["abc", "g" * 64, "a" * 63, "a" * 65, ""])
    def test_it_refuses_a_bad_digest(self, srv: Path, shim: Path, digest: str) -> None:
        assert run_direct(srv, shim, digest=digest).returncode != 0
        assert not (srv / "compose" / "metabase-jar.pin").exists()

    @pytest.mark.parametrize("version", ["0.62.19", "v1;rm", "v", "v0.62.19 x", ""])
    def test_it_refuses_a_bad_version(self, srv: Path, shim: Path, version: str) -> None:
        assert run_direct(srv, shim, version=version).returncode != 0

    def test_what_it_writes_is_what_the_server_reads(self, srv: Path, shim: Path) -> None:
        from dango.platform.cloud.metabase_pin import digest_for_version
        from dango.platform.cloud.server_compose import Layout

        assert run_direct(srv, shim, digest="B" * 64).returncode == 0
        assert digest_for_version(CUSTOM, Layout(srv=str(srv), owner_uid=os.getuid())) == "b" * 64


def test_the_cli_prints_a_long_command_unbroken() -> None:
    from click.testing import CliRunner

    from dango.cli.commands.remote import remote
    from dango.platform.cloud.upgrade_preflight import recovery

    command = recovery(CUSTOM, helper_usable=False)
    assert len(command) > 200
    cfg = MagicMock(droplet_ip="1.2.3.4", provider="digitalocean")
    with (
        patch("dango.cli.utils.require_project_context", return_value=Path("/project")),
        patch(
            "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
            return_value=(cfg, MagicMock()),
        ),
        patch("dango.platform.cloud.upgrade.check_versions", return_value=("1.0.13", "1.0.14")),
        patch(
            "dango.platform.cloud.upgrade.upgrade_dango",
            side_effect=CloudError(f"Upgrade refused. As root run: {command}  Then retry."),
        ),
    ):
        result = CliRunner().invoke(
            remote,
            ["upgrade", "--version", "1.0.14", "--yes"],
            obj={"project_root": Path("/project")},
        )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert command in plain  # exactly, no inserted line breaks


# --- L2: the pre-flight agrees with the server --------------------------------------------------------


@dataclass
class StrictServer(FakeServer):
    """``FakeServer`` that answers the pre-flight read in its output format."""

    dockerfile: bytes | None = None
    dockerfile_state: str | None = None
    pin: bytes | None = None
    helper_usable: bool = False

    def _answer(self, command: str) -> CommandResult:
        if "Dockerfile.metabase" in command and "metabase-jar.pin" in command:
            self.events.append("preflight")
            state = self.dockerfile_state or ("absent" if self.dockerfile is None else "file")
            pinhex = "" if self.pin is None else self.pin.hex()
            head = f"helper={int(self.helper_usable)}\npinhex={pinhex}\ndockerfile={state}\n---\n"
            return CommandResult(head + (self.dockerfile or b"").decode("latin-1"), "", 0)
        return super()._answer(command)


def pin_bytes(version: str = CUSTOM, digest: str = DIGEST, end: str = "\n") -> bytes:
    return f"{version} {digest}{end}".encode()


DOCKERFILES: dict[str, bytes | None] = {
    "absent": None,
    "default": b"ENV MB_VERSION=v0.62.18\n",
    "custom": b"FROM x\nENV MB_VERSION=v0.62.19\n",
    "quoted": b'ENV MB_VERSION="v0.62.19"\n',
    "trailing blanks": b"ENV MB_VERSION=v0.62.19  \t\n",
    "crlf": b"ENV MB_VERSION=v0.62.19\r\n",
    "no final newline": b"ENV MB_VERSION=v0.62.19",
    "lowercase env": b"env MB_VERSION=v0.62.19\n",
    "legacy space form": b"ENV MB_VERSION v0.62.19\n",
    "no v prefix": b"ENV MB_VERSION=0.62.19\n",
    "conflicting": b"ENV MB_VERSION=v0.62.19\nENV MB_VERSION=v0.62.20\n",
    "twice the same": b"ENV MB_VERSION=v0.62.19\nENV MB_VERSION=v0.62.19\n",
    "indented": b"  ENV MB_VERSION=v0.62.19\n",
    "extra words": b"ENV MB_VERSION=v0.62.19 EXTRA=1\n",
    "no version at all": b"FROM x\n",
    "empty file": b"",
}
PINS: dict[str, bytes | None] = {
    "none": None,
    "valid": pin_bytes(),
    "no newline": pin_bytes(end=""),
    "crlf": pin_bytes(end="\r\n"),
    "trailing blank": pin_bytes(end=" \n"),
    "two newlines": pin_bytes(end="\n\n"),
    "upper-case digest": pin_bytes(digest="A" * 64),
    "short digest": pin_bytes(digest="a" * 63),
    "other version": pin_bytes(version="v0.62.20"),
    "no v": pin_bytes(version="0.62.19"),
    "leading blank": b" " + pin_bytes(),
    "two records": pin_bytes() + pin_bytes(version="v0.62.20"),
    "garbage": b"garbage\n",
    "too big": pin_bytes(end="\n" + "x" * 130),
}


def server_refuses(dockerfile: bytes | None, pin: bytes | None) -> bool:
    """What the server's own render would do, using its real parsers on a temp tree."""
    from dango.platform.cloud.metabase_pin import DEFAULT_MB_VERSION, PinError, digest_for_version
    from dango.platform.cloud.server_compose import Layout
    from dango.platform.compose_exec import ComposeRenderError, _project_mb_version

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw).resolve()
        project = root / "project"
        project.mkdir()
        if dockerfile is not None:
            (project / "Dockerfile.metabase").write_bytes(dockerfile)
        try:
            version = _project_mb_version(project / "Dockerfile.metabase") or DEFAULT_MB_VERSION
        except ComposeRenderError:
            return True
        compose = root / "compose"
        compose.mkdir(mode=0o755)
        if pin is not None:
            (compose / "metabase-jar.pin").write_bytes(pin)
            (compose / "metabase-jar.pin").chmod(0o600)
        try:
            digest_for_version(version, Layout(srv=str(root), owner_uid=os.getuid()))
        except PinError:
            return True
        return False


def preflight_refuses(dockerfile: bytes | None, pin: bytes | None) -> bool:
    srv = StrictServer(version="1.0.13", dockerfile=dockerfile, pin=pin, helper_usable=True)
    with patch("dango.platform.cloud.backup.create_backup", return_value=MagicMock()):
        try:
            upgrade_dango(srv, version="1.0.14")
        except CloudError as exc:
            assert srv.version == "1.0.13" and "web-stop" not in srv.events, str(exc)
            return True
    return False


@pytest.fixture(autouse=True)
def _env() -> Iterator[None]:
    with (
        patch("dango.platform.cloud.runtime_ownership.venv_trust_findings", return_value=[]),
        patch("dango.platform.cloud.backup.verify_health", return_value=True),
        patch("dango.platform.cloud.server_status.local_client_version", return_value="1.0.14"),
    ):
        yield


@pytest.mark.parametrize("pin_name", list(PINS))
@pytest.mark.parametrize("dockerfile_name", list(DOCKERFILES))
def test_the_preflight_refuses_exactly_what_the_server_would(
    dockerfile_name: str, pin_name: str
) -> None:
    dockerfile, pin = DOCKERFILES[dockerfile_name], PINS[pin_name]
    assert preflight_refuses(dockerfile, pin) == server_refuses(dockerfile, pin)


@pytest.mark.parametrize("state", ["bad"])
def test_an_unreadable_project_dockerfile_is_refused_up_front(state: str) -> None:
    """The server opens it without following links and wants a regular file."""
    srv = StrictServer(
        version="1.0.13", dockerfile=b"ENV MB_VERSION=v0.62.18\n", dockerfile_state=state
    )
    with pytest.raises(CloudError, match="Dockerfile.metabase") as excinfo:
        upgrade_dango(srv, version="1.0.14")
    assert "not stopped" in str(excinfo.value) and srv.version == "1.0.13"


def test_an_unusable_dockerfile_message_says_what_to_fix() -> None:
    srv = StrictServer(version="1.0.13", dockerfile=b"env MB_VERSION=v0.62.19\n")
    with pytest.raises(CloudError) as excinfo:
        upgrade_dango(srv, version="1.0.14")
    message = str(excinfo.value)
    assert "Dockerfile.metabase" in message and "ENV MB_VERSION=" in message
    assert "pin-metabase" not in message


def test_the_preflight_shares_the_servers_parsers() -> None:
    from dango.platform import compose_exec
    from dango.platform.cloud import metabase_pin
    from dango.platform.cloud import upgrade_preflight as up

    text = Path(up.__file__).read_text()
    assert "_PIN_RECORD" not in text and "re.compile" not in text
    assert callable(metabase_pin.parse_pin_record) and callable(compose_exec.mb_version_of)


# --- L3: server text in the upgrade output ---------------------------------------------------------------

HOSTILE = "find: '/tmp/nb/[/x]': Permission denied [bold]"


def _result(**kw: Any) -> UpgradeResult:
    base: dict[str, Any] = {
        "old_version": "1.0.13",
        "new_version": "1.0.14",
        "backup_path": "/srv/dango/backups/[/x].tar.gz",
        "migrations_run": True,
        "docker_rebuilt": True,
        "health_check_passed": True,
        "duration_seconds": 3.0,
        "warnings": [HOSTILE],
        "caddyfile_ws_migration": f"ws {HOSTILE}",
        "runtime_ownership": f"own {HOSTILE}",
        "venv_rebuild": f"venv {HOSTILE}",
        "runner_isolation": f"iso {HOSTILE}",
    }
    base.update(kw)
    return UpgradeResult(**base)


def _invoke(args: list[str], upgrade: Any) -> Any:
    from click.testing import CliRunner

    from dango.cli.commands.remote import remote

    cfg = MagicMock(droplet_ip="1.2.3.4", provider="digitalocean")
    with (
        patch("dango.cli.utils.require_project_context", return_value=Path("/project")),
        patch(
            "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
            return_value=(cfg, MagicMock()),
        ),
        patch("dango.platform.cloud.upgrade.check_versions", return_value=("1.0.13", "1.0.14")),
        patch("dango.platform.cloud.upgrade.upgrade_dango", upgrade),
    ):
        return CliRunner().invoke(remote, args, obj={"project_root": Path("/project")})


def test_upgrade_output_keeps_server_text_literal() -> None:
    result = _invoke(["upgrade", "--version", "1.0.14", "--yes"], MagicMock(return_value=_result()))
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert result.exit_code == 0, result.output
    assert "Upgrade complete" in plain and "Upgrade failed" not in plain
    for fragment in ("ws ", "own ", "venv ", "iso ", "Warning:", "Backup: /srv/dango/backups/[/x]"):
        assert fragment in plain
    assert plain.count("[/x]") >= 6 and "[bold]" in plain


def test_upgrade_failure_text_stays_literal_and_whole() -> None:
    result = _invoke(
        ["upgrade", "--version", "1.0.14", "--yes"],
        MagicMock(side_effect=CloudError(f"broke {HOSTILE}")),
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    assert result.exit_code == 1 and f"broke {HOSTILE}" in plain


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _resize(tmp_path: Path, resize: Any) -> Any:
    from tests.unit.test_remote_ops_cli import (
        _PATCH_LOADER,
        _PATCH_REQUIRE_CTX,
        _PATCH_SSH_CLS,
        _make_cloud_config,
        _make_loader,
        _make_ssh_mock,
        _run,
    )

    with (
        patch(_PATCH_REQUIRE_CTX, return_value=tmp_path),
        patch(_PATCH_LOADER, return_value=_make_loader(_make_cloud_config())),
        patch(_PATCH_SSH_CLS, return_value=_make_ssh_mock()),
        patch("dango.platform.cloud.resize.resize_droplet", resize),
        patch("dango.platform.cloud.digitalocean.DigitalOceanClient", return_value=MagicMock()),
    ):
        return _run(["resize", "s-4vcpu-8gb", "--yes"], tmp_path, catch_exceptions=True)


def test_resize_output_and_failure_keep_server_text_literal(tmp_path: Path) -> None:
    from dango.platform.cloud.resize import ResizeResult

    done = ResizeResult(
        old_size="s-2vcpu-4gb",
        new_size="s-4vcpu-8gb",
        old_tier="Standard",
        new_tier="Performance",
        duration_seconds=1.0,
        backup_path="/srv/[/x].tar.gz",
        dbt_profiles_regenerated=False,
        warnings=[HOSTILE],
    )
    ok = _resize(tmp_path, MagicMock(return_value=done))
    bad = _resize(tmp_path, MagicMock(side_effect=CloudError(f"broke {HOSTILE}")))
    assert "Resize complete" in ok.output and HOSTILE in _plain(ok.output)
    assert bad.exit_code == 1 and f"broke {HOSTILE}" in _plain(bad.output)


# --- L4: the installed version is only believed when it is unambiguous ---------------------------------------


def _parsed(extra: str) -> iso.IsolationStatus:
    return iso.parse_isolation_probe(probe().replace("sudoers=1\n", f"sudoers=1\n{extra}\n"))


def test_one_installed_directory_is_believed() -> None:
    assert _parsed("dists=1\ninstalled=1.0.13").state == "unsupported"
    assert _parsed("dists=1\ninstalled=1.0.14").state == "on"


@pytest.mark.parametrize("extra", ["dists=2\ninstalled=", "dists=3\ninstalled=1.0.14", "dists=2"])
def test_several_installed_directories_never_show_on(extra: str) -> None:
    status = _parsed(extra)
    assert status.state == "unknown"
    assert iso.format_isolation_line(status) == "unknown"


def test_a_server_with_no_installed_directory_is_not_called_unsupported() -> None:
    assert _parsed("dists=0\ninstalled=").state == "on"  # the markers decide; nothing contradicts


def _fragment() -> str:
    cmd = iso.ISOLATION_PROBE_CMD
    return cmd[cmd.index("c=0; for p in") : cmd.index("echo ---")]


def run_fragment(tmp_path: Path, *dist_infos: str) -> dict[str, str]:
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for name in dist_infos:
        (site / name).mkdir()
    script = _fragment().replace("/srv/dango/venv", str(tmp_path / "venv"))
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False)
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def test_the_probe_counts_the_directories_and_names_only_a_single_one(tmp_path: Path) -> None:
    assert run_fragment(tmp_path / "one", "getdango-1.0.13.dist-info") == {
        "dists": "1",
        "installed": "1.0.13",
    }
    many = run_fragment(tmp_path / "many", "getdango-1.0.9.dist-info", "getdango-1.0.14.dist-info")
    assert many["dists"] == "2" and many.get("installed", "") == ""
    assert run_fragment(tmp_path / "none")["dists"] == "0"
