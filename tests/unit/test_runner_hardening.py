"""tests/unit/test_runner_hardening.py

Review-round hardening of the runner-isolation wiring: the notebook ACL pass cannot be raced,
nothing runs as a runner while permissions are re-applied, pushed code is never writable by
others, the firewall also covers the Docker bridge and the host's own addresses, root refuses to
work under a writable ``/srv/dango``, and the protected paths exist (denied) before anything writes
them.  Shell text is run under dash against a temp tree with recording fake tools.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as iso
from dango.platform.cloud import runner_scripts as rs

pytestmark = pytest.mark.unit

DASH = shutil.which("dash") or "/bin/sh"


class Box:
    """A temp server tree plus recording fake tools first on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.write_text("")
        self.srv = root / "srv" / "dango"
        self.proj = self.srv / "project"
        self.proj.mkdir(parents=True)
        self.tool("setfacl", 'echo "setfacl $*" >>"$LOG"\n')

    def tool(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        full = {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "LOG": str(self.log),
            "HOME": str(self.root),
            **env,
        }
        return subprocess.run(
            [DASH, "-c", script], capture_output=True, text=True, env=full, cwd=self.root
        )

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# --- H1: the notebooks ACL pass -----------------------------------------------------------------


def test_notebooks_acl_has_no_path_based_exec() -> None:
    text = access.notebooks_acl_script()
    assert "-execdir" in text and "find -P" in text
    assert "-exec " not in text.replace("-execdir ", "")


def test_notebooks_acl_hands_setfacl_only_names_inside_its_own_directory(box: Box) -> None:
    nb = box.proj / "notebooks"
    (nb / "sub").mkdir(parents=True)
    (nb / "a.py").write_text("x")
    (nb / "sub" / "b.py").write_text("x")
    victim = box.proj / "custom_sources"
    victim.mkdir()
    (victim / "src.py").write_text("x")
    (nb / "linkdir").symlink_to(victim)
    (nb / "linkfile.py").symlink_to(victim / "src.py")
    box.tool("setfacl", 'echo "setfacl|$(pwd -P)|$*" >>"$LOG"\n')
    res = box.run(access.notebooks_acl_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    file_calls = [c.split("|") for c in box.calls() if c.startswith("setfacl|") and "-P" in c]
    assert file_calls, box.calls()
    seen = set()
    for _, cwd, args in file_calls:
        names = args.split()[3:]  # after -P -m <acl>
        for name in names:
            assert name.startswith("./") and "/" not in name[2:], name
            seen.add(os.path.join(cwd, name[2:]))
    real = os.path.realpath
    assert seen == {real(nb / "a.py"), real(nb / "sub" / "b.py")}
    assert not any("src.py" in c or "custom_sources" in c for c in box.calls())


def test_a_directory_swapped_after_listing_cannot_redirect_the_acl(box: Box) -> None:
    """The fake setfacl plays the racer: on its first call it swaps ``sub`` for a link."""
    nb = box.proj / "notebooks"
    (nb / "sub").mkdir(parents=True)
    (nb / "a.py").write_text("x")
    (nb / "sub" / "b.py").write_text("x")
    victim = box.proj / "custom_sources"
    victim.mkdir()
    (victim / "b.py").write_text("secret")
    marker = box.root / "swapped"
    box.tool(
        "setfacl",
        f'case "$*" in *rwx*) exit 0;; esac\n'
        f'echo "setfacl|$(pwd -P)|$*" >>"$LOG"\n'
        f"if [ ! -e {marker} ]; then : >{marker}; mv {nb}/sub {nb}/sub.real;"
        f" ln -s {victim} {nb}/sub; fi\n",
    )
    res = box.run(access.notebooks_acl_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    for call in box.calls():
        _, cwd, args = call.split("|")
        assert os.path.realpath(victim) not in cwd, call
        assert all(a.startswith("./") for a in args.split()[3:]), call


def test_quiesce_comes_before_every_permission_step() -> None:
    names = [s.name for s in iso.build_steps()]
    assert names.index("quiesce") < names.index("project_permissions")
    assert names.index("quiesce") < names.index("notebooks_acl")


def _quiesce_box(box: Box, survivors: int) -> None:
    counter = box.root / "n"
    counter.write_text(str(survivors))
    box.tool("getent", "exit 0\n")
    box.tool("sleep", "exit 0\n")
    box.tool("pkill", 'echo "pkill $*" >>"$LOG"\nn=$(cat "$N"); echo $((n-1)) >"$N"\n')
    box.tool("pgrep", 'n=$(cat "$N"); [ "$n" -gt 0 ]\n')
    box.tool(
        "systemctl",
        'echo "systemctl $*" >>"$LOG"\n[ "$1" = is-active ] && exit 3\nexit 0\n',
    )


def test_quiesce_stops_web_then_kills_runner_processes_and_checks_they_are_gone(box: Box) -> None:
    _quiesce_box(box, survivors=2)
    res = box.run(access.quiesce_script(stop_web=True), N=str(box.root / "n"))
    assert res.returncode == 0, res.stderr
    calls = box.calls()
    assert calls[0] == "systemctl stop dango-web"
    assert any(c == "pkill -KILL -u dango-nb" for c in calls)
    assert any(c == "pkill -KILL -u dango-run" for c in calls)


def test_quiesce_fails_when_a_runner_process_survives(box: Box) -> None:
    _quiesce_box(box, survivors=10**6)
    res = box.run(access.quiesce_script(stop_web=False), N=str(box.root / "n"))
    assert res.returncode == 8 and "dango-" in res.stderr
    assert not any(c.startswith("systemctl stop") for c in box.calls())


def test_quiesce_fails_when_web_is_still_running(box: Box) -> None:
    _quiesce_box(box, survivors=0)
    box.tool("systemctl", 'echo "systemctl $*" >>"$LOG"\nexit 0\n')
    res = box.run(access.quiesce_script(stop_web=True), N=str(box.root / "n"))
    assert res.returncode == 8 and "dango-web" in res.stderr


def test_quiesce_skips_users_that_do_not_exist_yet(box: Box) -> None:
    _quiesce_box(box, survivors=5)
    box.tool("getent", "exit 2\n")
    res = box.run(access.quiesce_script(stop_web=False), N=str(box.root / "n"))
    assert res.returncode == 0 and not any("pkill" in c for c in box.calls())


# --- H2: code that is pushed -------------------------------------------------------------------


def _modes(root: Path) -> dict[str, int]:
    out = {".": stat.S_IMODE(root.stat().st_mode)}
    for p in root.rglob("*"):
        if not p.is_symlink():
            out[str(p.relative_to(root))] = stat.S_IMODE(p.stat().st_mode)
    return out


def test_normalised_copy_removes_other_and_group_write_and_keeps_readability(
    tmp_path: Path,
) -> None:
    from dango.platform.cloud.rsync_transport import _normalised_copy

    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.py").write_text("x")
    (src / "run.sh").write_text("x")
    (src / "sub" / "b.sql").write_text("x")
    (src / "private").mkdir()
    (src / "private" / "c.py").write_text("x")
    (src / "ln").symlink_to("a.py")
    os.chmod(src / "a.py", 0o777)
    os.chmod(src / "run.sh", 0o700)
    os.chmod(src / "sub", 0o777)
    os.chmod(src / "sub" / "b.sql", 0o666)
    os.chmod(src / "private", 0o700)
    os.chmod(src / "private" / "c.py", 0o600)
    before = _modes(src)
    out = tmp_path / "out"
    out.mkdir()
    staged = _normalised_copy(src, out)
    modes = _modes(staged)
    assert modes["a.py"] == 0o755  # had an execute bit
    assert modes["run.sh"] == 0o755
    assert modes["sub"] == 0o755 and modes["private"] == 0o755 and modes["."] == 0o755
    assert modes["sub/b.sql"] == 0o644 and modes["private/c.py"] == 0o644
    assert (staged / "ln").is_symlink() and os.readlink(staged / "ln") == "a.py"
    assert _modes(src) == before  # the project itself is untouched


def test_rsync_sends_the_normalised_copy_never_the_project_modes(tmp_path: Path) -> None:
    from dango.platform.cloud.rsync_transport import _rsync_directory

    src = tmp_path / "scripts"
    src.mkdir()
    (src / "a.py").write_text("x")
    os.chmod(src / "a.py", 0o777)
    os.chmod(src, 0o777)
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        source = Path(cmd[-2].rstrip("/"))
        seen["source"] = source
        seen["modes"] = _modes(source)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with (
        patch("dango.platform.cloud.rsync_transport._require_pinned_host"),
        patch("dango.platform.cloud.rsync_transport.subprocess.run", side_effect=fake_run),
    ):
        _rsync_directory(
            src,
            "root@10.0.0.1:/srv/dango/project/scripts",
            Path("/k"),
            known_hosts_path=Path("/kh"),
        )
    assert seen["source"] != src
    assert seen["modes"] == {".": 0o755, "a.py": 0o755}
    assert not Path(str(seen["source"])).exists()  # staging is removed afterwards
    assert stat.S_IMODE(src.stat().st_mode) == 0o777


CODE_CHECK_HEAD = 'set -u\nbad=0\nfail() { echo "verify: $*" >&2; bad=1; }\n'


def _code_check(box: Box) -> subprocess.CompletedProcess[str]:
    text = access.code_permission_checks(project=str(box.proj), srv_dir=str(box.srv))
    return box.run(CODE_CHECK_HEAD + text + '\nexit "$bad"\n')


def _runner_tools(box: Box, writable: bool = False) -> None:
    box.tool(
        "runuser", 'shift 3\n"$@"\n' if writable else 'shift 3\n[ "$1" = test ] && exit 1\n"$@"\n'
    )


def test_verify_flags_other_writable_code(box: Box) -> None:
    _runner_tools(box)
    for d in ("scripts", "custom_sources", "dbt", ".dango"):
        (box.proj / d).mkdir()
        os.chmod(box.proj / d, 0o755)
    (box.srv / "venv").mkdir()
    assert _code_check(box).returncode == 0
    bad = box.proj / "custom_sources" / "x.py"
    bad.write_text("x")
    os.chmod(bad, 0o666)
    res = _code_check(box)
    assert res.returncode == 1 and "x.py" in res.stderr


def test_verify_flags_a_group_writable_venv_and_runner_writable_dirs(box: Box) -> None:
    _runner_tools(box)
    (box.proj / "scripts").mkdir()
    (box.srv / "venv").mkdir()
    (box.srv / "venv" / "f").write_text("x")
    os.chmod(box.srv / "venv" / "f", 0o664)
    assert "venv" in _code_check(box).stderr
    os.chmod(box.srv / "venv" / "f", 0o644)
    assert _code_check(box).returncode == 0
    _runner_tools(box, writable=True)
    res = _code_check(box)
    assert res.returncode == 1 and "can write" in res.stderr


# --- M1: the firewall ---------------------------------------------------------------------------


def test_nft_rejects_the_docker_bridge_and_every_local_address() -> None:
    text = rs.NFT_RULESET
    both = 'meta skuid { "dango-run", "dango-nb" }'
    assert f'{both} oifname {{ "docker0", "br-*" }} reject' in text
    assert f"{both} fib daddr type local reject" in text
    lines = text.splitlines()
    accept = [i for i, ln in enumerate(lines) if "accept" in ln and "skuid" in ln]
    reject = [i for i, ln in enumerate(lines) if "reject" in ln]
    assert max(accept) < min(reject)
    # DNS and the notebook's own port stay allowed
    assert f"{both} ip daddr 127.0.0.0/8 udp dport 53 accept" in text
    assert 'meta skuid "dango-nb" ip daddr 127.0.0.1 tcp dport 7805 accept' in text


def test_the_restricted_state_is_described_as_covering_more_than_loopback() -> None:
    assert "Docker" in rs.LOOPBACK_DOC and "loopback" in rs.LOOPBACK_DOC
    assert "Docker" in iso.NFT_WARNING


# --- M2: root refuses a writable /srv/dango ----------------------------------------------------


def _precheck(box: Box) -> subprocess.CompletedProcess[str]:
    me = subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()
    text = access.ownership_precheck_script(
        str(box.srv), owner=me, paths=[str(box.srv.parent), str(box.srv)]
    )
    return box.run(text)


def test_precheck_passes_for_trusted_directories(box: Box) -> None:
    os.chmod(box.srv.parent, 0o755)
    os.chmod(box.srv, 0o755)
    assert _precheck(box).returncode == 0


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o770 | 0o002])
def test_precheck_refuses_a_group_or_other_writable_directory(box: Box, mode: int) -> None:
    os.chmod(box.srv.parent, 0o755)
    os.chmod(box.srv, mode)
    res = _precheck(box)
    assert res.returncode == 7 and str(box.srv) in res.stderr


def test_precheck_refuses_a_symlink_and_a_missing_path(box: Box) -> None:
    os.chmod(box.srv.parent, 0o755)
    os.chmod(box.srv, 0o755)
    link = box.root / "link"
    link.symlink_to(box.srv)
    text = access.ownership_precheck_script(
        str(link),
        owner=subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip(),
        paths=[str(link)],
    )
    assert box.run(text).returncode == 7
    gone = access.ownership_precheck_script(str(box.root / "none"), paths=[str(box.root / "none")])
    assert box.run(gone).returncode == 7


def test_precheck_wrong_owner_is_refused(box: Box) -> None:
    os.chmod(box.srv, 0o755)
    text = access.ownership_precheck_script(
        str(box.srv), owner="no-such-account", paths=[str(box.srv)]
    )
    assert box.run(text).returncode == 7


def test_precheck_is_the_first_step_and_checks_root_srv_and_srv_dango() -> None:
    steps = iso.build_steps()
    assert steps[0].name == "ownership_check" and steps[0].on_failure == "fatal"
    assert steps[0].run_as == "root"
    text = steps[0].script
    assert "for p in / /srv /srv/dango; do" in text
    assert "-user root" in text and "-perm -0020" in text and "-perm -0002" in text


# --- the known gap: protected paths exist, denied, before anything writes them -----------------


def test_project_script_creates_missing_protected_paths_with_the_right_modes(box: Box) -> None:
    (box.srv / "runner" / "snapshots").mkdir(parents=True)
    res = box.run(access.project_permission_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr

    def mode(name: str) -> int:
        return stat.S_IMODE((box.proj / name).stat().st_mode)

    assert mode(".dango") == 0o700 and mode(".dango/state") == 0o700 and mode(".dlt") == 0o700
    assert mode(".env") == 0o600 and (box.proj / ".env").read_text() == ""
    assert (box.proj / "data").is_dir() and (box.proj / "notebooks").is_dir()
    calls = box.calls()
    deny = "u:dango-run:---,u:dango-nb:---"
    for p in (".dango", ".dlt", ".env"):
        assert f"setfacl -m {deny} {box.proj}/{p}" in calls
    assert f"setfacl -m u:dango-nb:--- {box.proj}/data" in calls


def test_project_script_keeps_existing_content_and_a_data_symlink_target(box: Box) -> None:
    (box.srv / "runner" / "snapshots").mkdir(parents=True)
    (box.proj / ".env").write_text("A=1\n")
    target = box.root / "block"
    target.mkdir()
    (box.proj / "data").symlink_to(target)
    res = box.run(access.project_permission_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    assert (box.proj / ".env").read_text() == "A=1\n"
    assert (box.proj / "data").is_symlink()
    assert f"setfacl -m u:dango-nb:--- {os.path.realpath(target)}" in box.calls()


def test_project_script_leaves_a_dangling_data_link_alone(box: Box) -> None:
    (box.srv / "runner" / "snapshots").mkdir(parents=True)
    (box.proj / "data").symlink_to(box.root / "missing")
    res = box.run(access.project_permission_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    assert (box.proj / "data").is_symlink() and not (box.root / "missing").exists()


def test_verify_checks_that_protected_paths_are_actually_closed_to_the_runners() -> None:
    text = access.verify_script()
    assert 'stat -c %a "$p"' in text and "700" in text and "600" in text
    assert "runuser -u" in text and "can read" in text
    assert "dango-nb can reach data" in text or "can reach data" in text
