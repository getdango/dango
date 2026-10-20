"""tests/unit/test_compose_switch_scripts.py

The switch's generated shell scripts (group removal, process drop, downgrade restore, marker and version
record) are executed against a temp tree with fake tools, plus the client-side version notices.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud import compose_migrate as cm
from dango.platform.cloud.ssh import CommandResult
from tests.factories.compose_switch_fakes import FakeServer

pytestmark = pytest.mark.unit


def shim(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Sandbox:
    """A temp tree standing in for the server, with recording fake tools first on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.write_text("")
        (root / "srv" / "compose").mkdir(parents=True)
        (root / "etc" / "sudoers.d").mkdir(parents=True)
        (root / "proc").mkdir()
        for tool in ("chown", "chmod", "usermod", "gpasswd", "loginctl", "pkill", "sleep", "mv"):
            body = f'echo "{tool} $*" >>"{self.log}"\n'
            if tool == "mv":
                body = f'echo "mv $*" >>"{self.log}"\nexec /bin/mv "$@"\n'
            if tool == "chmod":
                body += 'exec /bin/chmod "$@"\n'
            shim(self.bin, tool, body)

    def tool(self, name: str, body: str) -> None:
        shim(self.bin, name, body)

    def adapt(self, script: str) -> str:
        script = script.replace(ch.SRV_DIR, str(self.root / "srv"))
        script = script.replace(
            ch.SUDOERS_PATH, str(self.root / "etc" / "sudoers.d" / "dango-compose")
        )
        return script.replace("/proc/", f"{self.root}/proc/")

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.root)}
        return subprocess.run(
            ["bash", "-c", self.adapt(script)], env=env, capture_output=True, text=True, check=False
        )

    def calls(self) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line]


@pytest.fixture
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


# --- helper_capable / below_helper_version --------------------------------------------------


@pytest.mark.parametrize(
    ("version", "capable"),
    [
        ("1.0.13", False),
        ("1.0.14", True),
        ("1.0.14rc1", True),
        ("1.0.15", True),
        ("1.1.0", True),
        ("0.9", False),
        (None, False),
    ],
)
def test_helper_capable(version: str | None, capable: bool) -> None:
    assert cm.helper_capable(version) is capable


@pytest.mark.parametrize(
    ("version", "below"),
    [("1.0.13", True), ("1.0.14rc1", False), ("1.0.14", False), ("1.0.9", True), ("2.0.0", False)],
)
def test_below_helper_version(version: str, below: bool) -> None:
    assert cm.below_helper_version(version) is below


# --- group removal (G) ------------------------------------------------------------------------


def test_drop_group_removes_a_member(box: Sandbox) -> None:
    box.tool("id", 'if [ -f "$0.done" ]; then echo dango; else echo "dango docker"; fi\n')
    box.tool("gpasswd", f'echo "gpasswd $*" >>"{box.log}"; : >"{box.bin}/id.done"\n')
    done = box.run(cm.drop_group_script())
    assert done.returncode == 0, done.stderr
    assert "gpasswd -d dango docker" in box.calls()


def test_drop_group_is_a_noop_for_a_non_member(box: Sandbox) -> None:
    box.tool("id", "echo dango\n")
    assert box.run(cm.drop_group_script()).returncode == 0
    assert not [c for c in box.calls() if c.startswith("gpasswd")]


def test_drop_group_fails_when_the_account_is_still_a_member(box: Sandbox) -> None:
    box.tool("id", "echo dango docker\n")
    assert box.run(cm.drop_group_script()).returncode != 0


# --- process drop (T) ---------------------------------------------------------------------------


def fake_proc(box: Sandbox, pid: int, *, uid: int, groups: str, state: str = "S") -> None:
    d = box.root / "proc" / str(pid)
    d.mkdir()
    (d / "status").write_text(
        f"State:\t{state} (x)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGroups:\t{groups}\n"
    )


def term_tools(box: Sandbox, *, running: bool) -> None:
    box.tool("id", "echo 1001\n")
    box.tool("getent", "echo docker:x:998:\n")
    box.tool("pgrep", "exit 0\n" if running else "exit 1\n")
    box.tool("ps", "echo dango-process\n")


def test_terminate_succeeds_when_no_process_holds_the_docker_gid(box: Sandbox) -> None:
    term_tools(box, running=False)
    fake_proc(box, 10, uid=1001, groups="1001 27")
    fake_proc(box, 11, uid=0, groups="0 998")  # root's own docker gid does not matter
    done = box.run(cm.terminate_script())
    assert done.returncode == 0, done.stderr
    assert "T_OK" in done.stdout
    assert not [c for c in box.calls() if c.startswith("pkill")]


def test_terminate_kills_in_order_and_reports_survivors(box: Sandbox) -> None:
    term_tools(box, running=True)
    fake_proc(box, 20, uid=1001, groups="1001 998")
    done = box.run(cm.terminate_script())
    assert done.returncode != 0
    assert "T_REMAIN" in done.stdout
    calls = [c for c in box.calls() if c.startswith(("loginctl", "pkill"))]
    assert calls[0] == "loginctl terminate-user dango"
    first_term = calls.index("pkill -TERM -u dango")
    first_stop = calls.index("pkill -STOP -u dango")
    first_kill = calls.index("pkill -KILL -u dango")
    assert 0 < first_term < first_stop < first_kill
    assert calls.count("loginctl terminate-user dango") == 3  # bounded rounds


def test_terminate_ignores_zombies(box: Sandbox) -> None:
    term_tools(box, running=False)
    fake_proc(box, 30, uid=1001, groups="1001 998", state="Z")
    assert "T_OK" in box.run(cm.terminate_script()).stdout


def test_terminate_waits_for_other_processes_before_killing(box: Sandbox) -> None:
    term_tools(box, running=True)
    fake_proc(box, 40, uid=1001, groups="1001 998")
    box.tool("sleep", f'echo "sleep $*" >>"{box.log}"\n')
    box.run(cm.terminate_script())
    calls = box.calls()
    assert calls.index("sleep 2") < calls.index("loginctl terminate-user dango")
    assert calls.count("sleep 2") >= 60  # polls for 120 s


# --- marker and version record ----------------------------------------------------------------


def test_marker_script_writes_marker_and_record(box: Sandbox) -> None:
    done = box.run(cm.marker_script("1.0.14", "migrating"))
    assert done.returncode == 0, done.stderr
    srv = box.root / "srv"
    assert (srv / "compose" / "active").is_file()
    assert json.loads((srv / "server-version.json").read_text()) == {
        "dango": "1.0.14",
        "docker_access": "migrating",
    }
    assert not list(srv.rglob("*.tmp"))
    assert any(c.startswith("chown root:root") for c in box.calls())


def test_marker_comes_after_the_record_is_staged(box: Sandbox) -> None:
    text = cm.marker_script("1.0.14", "migrating")
    assert text.index("server-version.json") < text.index(ch.MARKER)


def test_record_script_replaces_the_record(box: Sandbox) -> None:
    assert box.run(cm.record_script("1.0.14", "helper")).returncode == 0
    assert box.run(cm.record_script("1.0.15", "group")).returncode == 0
    data = json.loads((box.root / "srv" / "server-version.json").read_text())
    assert data == {"dango": "1.0.15", "docker_access": "group"}


@pytest.mark.parametrize("version", ["", "1.0; rm -rf /", "x" * 40, "$(id)", '1.0"'])
def test_record_rejects_unsafe_versions(version: str) -> None:
    with pytest.raises(ValueError):
        cm.record_script(version, "helper")


def test_record_rejects_unknown_access() -> None:
    with pytest.raises(ValueError):
        cm.record_script("1.0.14", "root")


# --- downgrade restore (D5, N1) -------------------------------------------------------------------


def restore_tools(
    box: Sandbox, *, member: bool, volume: bool = True, driver: str = "local", options: str = "0"
) -> Path:
    mount = (
        box.root / "var" / "lib" / "docker" / "volumes" / "dango-ab12cd34_metabase-data" / "_data"
    )
    mount.mkdir(parents=True)
    (box.root / "srv" / "compose" / "project").write_text("dango-ab12cd34\n")
    (box.root / "srv" / "compose" / "active").write_text("")
    (box.root / "etc" / "sudoers.d" / "dango-compose").write_text("rule\n")
    box.tool("id", "echo dango docker\n" if member else "echo dango\n")
    out = f"{driver}|{options}|{mount}"
    box.tool(
        "docker", "exit 1\n" if not volume else f'echo "docker $*" >>"{box.log}"; echo "{out}"\n'
    )
    return mount


def test_restore_returns_access_before_it_removes_the_helper(box: Sandbox) -> None:
    restore_tools(box, member=False)
    done = box.run(cm.restore_script("1.0.13"))
    assert done.returncode == 0, done.stderr
    calls = box.calls()
    assert "usermod -aG docker dango" in calls
    assert not (box.root / "srv" / "compose" / "active").exists()
    assert not (box.root / "etc" / "sudoers.d" / "dango-compose").exists()
    data = json.loads((box.root / "srv" / "server-version.json").read_text())
    assert data == {"dango": "1.0.13", "docker_access": "group"}


def test_restore_keeps_the_helper_when_the_group_cannot_be_restored(box: Sandbox) -> None:
    restore_tools(box, member=False)
    box.tool("usermod", "exit 1\n")
    assert box.run(cm.restore_script("1.0.13")).returncode != 0
    assert (box.root / "srv" / "compose" / "active").exists()
    assert (box.root / "etc" / "sudoers.d" / "dango-compose").exists()


def test_restore_returns_the_metabase_volume_to_the_old_uid(box: Sandbox) -> None:
    mount = restore_tools(box, member=True)
    assert box.run(cm.restore_script("1.0.13")).returncode == 0
    chowns = [c for c in box.calls() if c.startswith("chown") and "999" in c]
    assert chowns == [f"chown -hR 999:999 {mount}"]
    assert not [c for c in box.calls() if c.startswith("usermod")]  # already a member


@pytest.mark.parametrize(("driver", "options"), [("nfs", "0"), ("local", "2")])
def test_restore_refuses_to_chown_a_volume_with_driver_options(
    box: Sandbox, driver: str, options: str
) -> None:
    restore_tools(box, member=True, driver=driver, options=options)
    done = box.run(cm.restore_script("1.0.13"))
    assert "RESTORE_WARN" in done.stdout
    assert not [c for c in box.calls() if c.startswith("chown") and "999" in c]


def test_restore_without_a_volume_still_completes(box: Sandbox) -> None:
    restore_tools(box, member=True, volume=False)
    done = box.run(cm.restore_script("1.0.13"))
    assert done.returncode == 0
    assert not [c for c in box.calls() if c.startswith("chown") and "999" in c]


# --- ownership extra steps, web unit ----------------------------------------------------------------


def test_ownership_steps_cover_helper_sudoers_and_compose_dir() -> None:
    steps = cm.ownership_steps()
    assert [s.name for s in steps] == ["compose_helper"]
    text = steps[0].script
    for path in (ch.COMPOSE_DIR, ch.HELPER_PATH, ch.SUDOERS_PATH, ch.METABASE_DIR):
        assert path in text
    assert "is a symlink" in text
    assert "exit 1" in text  # the step fails when a path is still not root-owned


def test_web_unit_allows_the_helper_down_to_finish() -> None:
    from dango.platform.cloud._server_templates import build_systemd_unit

    unit = build_systemd_unit()
    stop = [line for line in unit.splitlines() if line.startswith("TimeoutStopSec=")]
    assert len(stop) == 1
    assert int(stop[0].split("=")[1].rstrip("s")) >= 180
    assert "NoNewPrivileges" not in unit


def test_refresh_web_unit_rewrites_an_old_unit_and_keeps_workers() -> None:
    old = "[Service]\nExecStart=/srv/dango/venv/bin/dango serve --host 127.0.0.1 --workers 3\n"
    srv = FakeServer(web_unit=old)
    warnings: list[str] = []
    cm.refresh_web_unit(srv, warnings)
    unit = srv.written["/etc/systemd/system/dango-web.service"]
    assert "TimeoutStopSec=" in unit and "--workers 3" in unit
    assert any("daemon-reload" in c for c in srv.commands)
    assert warnings == []


def test_refresh_web_unit_leaves_a_current_unit_alone() -> None:
    from dango.platform.cloud._server_templates import build_systemd_unit

    srv = FakeServer(web_unit=build_systemd_unit())
    cm.refresh_web_unit(srv, [])
    assert srv.written == {}


# --- client notices (V20) -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("local", "target", "shown"),
    [
        ("1.0.13", "1.0.14", True),
        ("1.0.14", "1.0.14", False),
        ("1.0.15", "1.0.14", False),
        ("1.0.14rc1", "1.0.14", True),
    ],
)
def test_client_upgrade_line(local: str, target: str, shown: bool) -> None:
    line = cm.client_upgrade_line(local, target)
    assert (line is not None) is shown
    if line:
        assert "Upgrade your local Dango" in line and "before upgrading the server" in line


def server_with_record(version: str | None) -> FakeServer:
    srv = FakeServer(marker=True, in_group=False)
    srv.record = json.dumps({"dango": version, "docker_access": "helper"}) if version else None
    return srv


def test_client_notice_warns_when_the_server_is_newer() -> None:
    notice = cm.client_version_notice(server_with_record("1.0.15"), "1.0.14")
    assert notice is not None and "1.0.15" in notice and "pip install -U getdango" in notice


@pytest.mark.parametrize("recorded", ["1.0.14", "1.0.13", None])
def test_client_notice_is_silent_otherwise(recorded: str | None) -> None:
    assert cm.client_version_notice(server_with_record(recorded), "1.0.14") is None


def test_client_notice_survives_a_failing_probe() -> None:
    class Broken:
        def exec_command(self, *_a: object, **_k: object) -> CommandResult:
            raise OSError("boom")

    assert cm.client_version_notice(Broken(), "1.0.14") is None


def test_scripts_carry_step_tags() -> None:
    tagged = {
        "H1": cm.prechecks_script(),
        "H3": cm.self_test_script(),
        "G": cm.drop_group_script(),
        "T": cm.terminate_script(),
        "M": cm.marker_script("1.0.14", "migrating"),
        "REC": cm.record_script("1.0.14", "helper"),
        "FB": cm.fallback_script("1.0.14"),
        "R": cm.restore_script("1.0.13"),
    }
    for tag, text in tagged.items():
        assert text.startswith(f"# step:{tag}\n"), tag


def test_self_test_failure_message_reaches_the_caller(box: Sandbox) -> None:
    message = "dango-compose: container id 61000 is already used on this host"
    box.tool("runuser", f'echo "{message}" >&2; exit 67\n')
    done = box.run(cm.self_test_script())
    assert done.returncode != 0
    assert message in done.stderr


def test_self_test_passes_when_ok_and_the_root_verb_is_refused(box: Sandbox) -> None:
    box.tool("runuser", 'case "$*" in *self-test*) echo ok;; *) exit 64;; esac\n')
    assert box.run(cm.self_test_script()).returncode == 0


def test_self_test_fails_when_a_root_verb_is_not_refused(box: Sandbox) -> None:
    box.tool("runuser", 'case "$*" in *self-test*) echo ok;; *) exit 0;; esac\n')
    assert box.run(cm.self_test_script()).returncode != 0
