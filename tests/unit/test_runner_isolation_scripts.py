"""tests/unit/test_runner_isolation_scripts.py

The runner-isolation shell builders: syntax, quoting, exact sudoers and firewall text, the
root-versus-service-user split, and real runs of the file-system scripts under dash against
a temp tree with recording fake tools (GNU-only commands are not executed here).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from dango.platform import run_as
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_isolation as iso
from dango.platform.cloud import runner_scripts as rs
from dango.platform.cloud._server_templates import build_systemd_unit

pytestmark = pytest.mark.unit

DASH = shutil.which("dash") or "/bin/sh"
HOSTILE = '/tmp/we ird/$(touch pwn)/`id`/it\'s "x";rm -rf'


def _all_scripts(srv: str = rs.SRV_DIR) -> dict[str, str]:
    out = {s.name: s.command for s in iso.build_steps(srv)}
    out["disable"] = rs.disable_script(srv)
    out["remove"] = rs.remove_script(srv)
    return out


@pytest.mark.parametrize("srv", [rs.SRV_DIR, HOSTILE])
def test_every_script_passes_syntax_check(srv: str) -> None:
    for name, text in _all_scripts(srv).items():
        for shell in ("sh", DASH):
            res = subprocess.run([shell, "-n", "-c", text], capture_output=True, text=True)
            assert res.returncode == 0, f"{name} under {shell}: {res.stderr}"


def test_installer_scripts_pass_syntax_check() -> None:
    for text in (rs.sudoers_install_script(), rs.nft_install_script()):
        res = subprocess.run([DASH, "-n", "-c", text], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


def test_hostile_paths_are_quoted_in_every_builder() -> None:
    quoted = rs.q(f"{HOSTILE}/runner")
    assert quoted in rs.runner_dirs_script(HOSTILE).replace(rs.q(f"{HOSTILE}/runner/scripts"), "")
    for text in (rs.runner_base_dir_script(HOSTILE), rs.marker_script(HOSTILE)):
        assert quoted in text
    assert rs.q(rs.marker_path(HOSTILE)) in rs.disable_script(HOSTILE)
    proj = rs.q(f"{HOSTILE}/project")
    assert f"P={proj}" in access.project_permission_script(srv_dir=HOSTILE)
    assert f"N={proj}/notebooks" in access.notebooks_acl_script(srv_dir=HOSTILE)
    assert f"P={proj}" in access.verify_script(srv_dir=HOSTILE)


class Sandbox:
    """A temp server tree plus recording fake tools first on PATH."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.log.write_text("")
        self.srv = root / "we ird" / "$(touch pwn)" / "it's"
        (self.srv / "project").mkdir(parents=True)
        self.tool("chown", 'echo "chown $*" >>"$LOG"\n')
        self.tool("setfacl", 'echo "setfacl $*" >>"$LOG"\n')
        self.tool("nft", 'echo "nft $*" >>"$LOG"\nexit ${NFT_RC:-0}\n')
        self.tool(
            "install",
            'echo "install $*" >>"$LOG"\nfor last; do :; done\nmkdir -p "$last"\n',
        )

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
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_base_dir_script_creates_the_runner_dir_without_recursion(box: Sandbox) -> None:
    res = box.run(rs.runner_base_dir_script(str(box.srv)))
    assert res.returncode == 0, res.stderr
    assert (box.srv / "runner").is_dir()
    assert any(c.startswith("install -d -o root -g root -m 0755") for c in box.calls())
    assert not (box.root / "pwn").exists()
    assert not any(" -R" in c for c in box.calls())


@pytest.mark.parametrize("child", ["", "scripts", "nb-home", "snapshots", "active"])
def test_dir_scripts_refuse_a_symlinked_target(box: Sandbox, child: str) -> None:
    runner = box.srv / "runner"
    runner.mkdir()
    victim = box.root / "victim"
    victim.mkdir()
    link = runner / child if child else runner
    if child:
        link.symlink_to(victim)
    else:
        runner.rmdir()
        runner.symlink_to(victim)
    texts = [rs.runner_base_dir_script(str(box.srv))]
    if child != "active":  # the marker path is guarded by the base-dir and marker scripts
        texts.append(rs.runner_dirs_script(str(box.srv)))
    for text in texts:
        res = box.run(text)
        assert res.returncode == 3, res.stderr
        assert "symlink" in res.stderr
    assert box.calls() == []
    assert list(victim.iterdir()) == []


def test_dirs_script_positive_control(box: Sandbox) -> None:
    (box.srv / "runner").mkdir()
    res = box.run(rs.runner_dirs_script(str(box.srv)))
    assert res.returncode == 0, res.stderr
    calls = "\n".join(box.calls())
    assert "chown dango:dango-nb" in calls and "dango-run:dango-run" in calls
    assert (box.srv / "runner" / "snapshots").is_dir()
    mode = stat.S_IMODE((box.srv / "runner" / "snapshots").stat().st_mode)
    assert mode == 0o2750


def test_dirs_script_never_recurses_or_touches_contents(box: Sandbox) -> None:
    text = rs.runner_dirs_script() + rs.runner_base_dir_script()
    assert not re.search(r"\s-R\b|--recursive|\bfind\b|setfacl|\brm\b", text)


@pytest.mark.parametrize("nft_rc, expect", [("0", "restricted"), ("1", "unrestricted")])
def test_marker_script_writes_a_trusted_record(box: Sandbox, nft_rc: str, expect: str) -> None:
    (box.srv / "runner").mkdir()
    res = box.run(rs.marker_script(str(box.srv)), NFT_RC=nft_rc)
    assert res.returncode == 0, res.stderr
    marker = box.srv / "runner" / "active"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o644
    assert not (box.srv / "runner" / ".active.tmp").exists()
    record = iso.read_record(str(marker), trusted_uid=os.getuid())
    assert record is not None and record.loopback == expect and record.schema == 1
    assert any(
        c == "chown root:root " + str(box.srv / "runner" / ".active.tmp") for c in box.calls()
    )


def test_marker_script_refuses_a_directory_or_symlink(box: Sandbox) -> None:
    runner = box.srv / "runner"
    (runner / "active").mkdir(parents=True)
    assert box.run(rs.marker_script(str(box.srv))).returncode == 3
    (runner / "active").rmdir()
    (runner / "active").symlink_to(box.root)
    assert box.run(rs.marker_script(str(box.srv))).returncode == 3


def test_disable_and_remove_scripts_drop_the_marker_first() -> None:
    assert rs.disable_script().strip().endswith(f"rm -f {rs.marker_path()}")
    removal = rs.remove_script()
    assert removal.index(rs.marker_path()) < removal.index(rs.SUDOERS_PATH)
    assert "userdel" not in removal and "-R" not in removal


def test_project_script_runs_against_a_temp_tree(box: Sandbox) -> None:
    proj = box.srv / "project"
    for name in (".dango", ".dlt", "data"):
        (proj / name).mkdir()
    (proj / ".env").write_text("A=1\n")
    (box.srv / "runner" / "snapshots").mkdir(parents=True)
    res = box.run(access.project_permission_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    assert stat.S_IMODE((proj / ".dango").stat().st_mode) == 0o700
    assert stat.S_IMODE((proj / ".env").stat().st_mode) == 0o600
    calls = box.calls()
    deny = "u:dango-run:---,u:dango-nb:---"
    assert f"setfacl -m {deny} {proj}/.env" in calls
    assert f"setfacl -m u:dango-nb:--- {proj}/data" in calls
    assert any(c.startswith("setfacl -m d:u::rwx,d:g::r-x") for c in calls)
    assert not (box.root / "pwn").exists()


def test_project_script_puts_the_data_acl_on_a_symlink_target(box: Sandbox) -> None:
    proj = box.srv / "project"
    target = box.root / "block-storage"
    target.mkdir()
    (proj / "data").symlink_to(target)
    (box.srv / "runner" / "snapshots").mkdir(parents=True)
    res = box.run(access.project_permission_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    resolved = os.path.realpath(target)
    assert f"setfacl -m u:dango-nb:--- {resolved}" in box.calls()


@pytest.mark.parametrize("name", [".dango", ".dlt", ".env", "notebooks"])
def test_project_script_refuses_a_symlinked_path(box: Sandbox, name: str) -> None:
    (box.srv / "runner" / "snapshots").mkdir(parents=True)
    (box.srv / "project" / name).symlink_to(box.root)
    res = box.run(access.project_permission_script(srv_dir=str(box.srv)))
    assert res.returncode == 3 and "symlink" in res.stderr
    assert box.calls() == []


def test_notebooks_script_refuses_a_symlinked_directory(box: Sandbox) -> None:
    (box.srv / "project" / "notebooks").symlink_to(box.root)
    res = box.run(access.notebooks_acl_script(srv_dir=str(box.srv)))
    assert res.returncode == 3 and box.calls() == []


def test_setfacl_failure_is_fatal_for_the_notebook_acl(box: Sandbox) -> None:
    box.tool("setfacl", "exit 1\n")
    res = box.run(access.notebooks_acl_script(srv_dir=str(box.srv)))
    assert res.returncode != 0


def test_notebooks_acl_does_not_follow_planted_links(box: Sandbox) -> None:
    nb = box.srv / "project" / "notebooks"
    nb.mkdir()
    (nb / "a.py").write_text("x")
    (nb / "b.py").symlink_to(box.root / "calls.log")
    res = box.run(access.notebooks_acl_script(srv_dir=str(box.srv)))
    assert res.returncode == 0, res.stderr
    calls = box.calls()
    assert any("setfacl -m u:dango-nb:rwx,d:u:dango-nb:rwx" in c for c in calls)
    file_calls = [c for c in calls if "-P" in c]
    assert len(file_calls) == 1 and file_calls[0].startswith("setfacl -P -m u:dango-nb:rw,m::rw")
    assert str(nb / "a.py") in file_calls[0] and "b.py" not in file_calls[0]


@pytest.mark.parametrize(
    "env, ok",
    [
        ({}, True),
        ({"RUN_UID": "999"}, False),
        ({"NB_GID": "61000"}, False),
        ({"RUN_UID": "61001"}, False),
        ({"RUN_UID": "999999"}, False),
        ({"RUN_UID": "500"}, False),
        ({"NB_UID": "1001"}, False),
        ({"SHELLV": "/bin/bash"}, False),
    ],
)
def test_users_script_enforces_the_id_rules(box: Sandbox, env: dict[str, str], ok: bool) -> None:
    box.tool(
        "id",
        'case "$2" in dango-run) n=RUN;; *) n=NB;; esac\n'
        'if [ "$1" = -u ]; then eval "echo \\$${n}_UID"; else eval "echo \\$${n}_GID"; fi\n',
    )
    box.tool("getent", 'echo "$2:x:1:1::/h:$SHELLV"\n')
    for tool in ("useradd", "passwd"):
        box.tool(tool, f'echo "{tool} $*" >>"$LOG"\n')
    base = {
        "RUN_UID": "1001",
        "RUN_GID": "1001",
        "NB_UID": "1002",
        "NB_GID": "1002",
        "SHELLV": rs.NOLOGIN,
    }
    res = box.run(rs.users_script(str(box.srv)), **{**base, **env})
    assert (res.returncode == 0) is ok, res.stderr
    if not ok:
        assert res.returncode == 5


def test_users_script_creates_missing_users_locked_and_without_groups() -> None:
    text = rs.users_script()
    assert text.count("useradd --no-create-home") == 2
    assert text.count("--shell /usr/sbin/nologin --user-group") == 2
    assert "passwd -l dango-run" in text and "passwd -l dango-nb" in text
    assert not re.search(r"usermod|gpasswd|adduser|\s-G\b|docker", text)


def test_sudoers_text_is_exact() -> None:
    assert rs.SUDOERS_TEXT == (
        "# Installed by Dango. The service account may run code as the restricted runner users,\n"
        "# never as root; the runner users get no sudo rule of their own.\n"
        "Defaults:dango !requiretty\n"
        "Defaults:dango !use_pty\n"
        "Cmnd_Alias DANGO_RUNNER = /srv/dango/venv/bin/python -I -m dango.runtime.isolated_run *\n"
        "Cmnd_Alias DANGO_SIGNAL = /usr/bin/kill\n"
        "dango ALL=(dango-run,dango-nb) NOPASSWD:SETENV: DANGO_RUNNER, DANGO_SIGNAL\n"
    )
    assert run_as.PYTHON in rs.SUDOERS_TEXT and run_as.KILL in rs.SUDOERS_TEXT


def test_sudoers_gives_the_runners_nothing() -> None:
    rules = [
        ln
        for ln in rs.SUDOERS_TEXT.splitlines()
        if ln and not ln.startswith(("#", "Defaults", "Cmnd"))
    ]
    assert rules == ["dango ALL=(dango-run,dango-nb) NOPASSWD:SETENV: DANGO_RUNNER, DANGO_SIGNAL"]
    assert "ALL=(root)" not in rs.SUDOERS_TEXT
    assert "SETENV" in rs.SUDOERS_TEXT


def test_sudoers_installer_validates_in_order_and_checks_the_rule() -> None:
    text = rs.sudoers_install_script()
    order = [
        "cat >/etc/sudoers.d/dango-runner.tmp",
        "chmod 0440",
        "visudo -cf /etc/sudoers.d/dango-runner.tmp",
        "mv -f /etc/sudoers.d/dango-runner.tmp /etc/sudoers.d/dango-runner",
        "visudo -c >/dev/null",
        "sudo -n -l -U dango",
    ]
    positions = [text.index(item) for item in order]
    assert positions == sorted(positions)
    assert "chown root:root /etc/sudoers.d/dango-runner.tmp" in text
    assert text.count("exit 4") >= 3
    assert "dango-compose" not in text


def test_web_unit_never_gets_no_new_privileges() -> None:
    assert "NoNewPrivileges" not in build_systemd_unit()
    assert "NoNewPrivileges" not in build_systemd_unit(workers=3)


def test_nft_ruleset_covers_both_users_and_the_dns_exceptions() -> None:
    text = rs.NFT_RULESET
    both = 'meta skuid { "dango-run", "dango-nb" }'
    assert f"{both} ip daddr 127.0.0.0/8 udp dport 53 accept" in text
    assert f"{both} ip daddr 127.0.0.0/8 tcp dport 53 accept" in text
    assert f"{both} ip daddr 127.0.0.0/8 reject" in text
    assert f"{both} ip6 daddr ::1 reject" in text
    assert 'meta skuid "dango-nb" ip daddr 127.0.0.1 tcp dport 7805 accept' in text
    assert "table inet dango_runner" in text and "policy accept" in text
    lines = text.splitlines()
    accept = [i for i, ln in enumerate(lines) if "accept" in ln and "skuid" in ln]
    reject = [i for i, ln in enumerate(lines) if "reject" in ln]
    assert max(accept) < min(reject)
    assert lines[1] == "add table inet dango_runner" and lines[2].startswith("delete table")


def test_nft_unit_is_its_own_oneshot() -> None:
    unit = rs.NFT_UNIT
    assert "Type=oneshot" in unit and "RemainAfterExit=yes" in unit
    assert f"ExecStart=/usr/sbin/nft -f {rs.NFT_PATH}" in unit
    assert rs.UNIT_PATH == "/etc/systemd/system/dango-runner-net.service"


def test_nft_installer_validates_first_and_leaves_system_firewall_alone() -> None:
    text = rs.nft_install_script()
    assert text.index("nft -c -f") < text.index("mv -f") < text.index("systemctl restart")
    assert not re.search(r"nftables\.conf|ufw|iptables|nftables\.service", text)
    assert "systemctl enable dango-runner-net.service" in text
    assert "list table inet dango_runner" in text


def test_package_scripts_install_only_when_missing() -> None:
    text = rs.package_script("acl", "setfacl")
    assert "command -v setfacl" in text and "apt-get install -y -qq acl" in text
    assert "exit 6" in text


_MUTATORS = re.compile(r"\b(chmod|chown|setfacl|install|mkdir|rm|mv|ln|cp|touch|tee|tar)\b")


def test_root_steps_never_act_on_project_paths_directly() -> None:
    for step in iso.build_steps():
        if step.run_as == "service":
            assert step.command.startswith("runuser -u dango -- sh -c ")
            assert not re.search(r"\bchown\b", step.script)
            continue
        if step.name == "verify":
            for line in step.script.splitlines():
                if "$P" in line and _MUTATORS.search(line):
                    assert "runuser -u dango --" in line, line
            continue
        assert "/srv/dango/project" not in step.script, step.name
        assert "$P" not in step.script, step.name


def test_root_scripts_only_mutate_runner_paths_users_sudoers_nft_and_marker() -> None:
    allowed = re.compile(
        r"/srv/dango/runner|/etc/sudoers\.d|/etc/nftables\.d|/etc/systemd/system|\$p\b|\$T\b|\"\$"
    )
    for step in iso.build_steps():
        if step.run_as != "root" or step.name in {"verify", "users"}:
            continue
        for line in step.script.splitlines():
            if re.match(r"\s*(chmod|chown|install -d|mv -f|rm -f)\b", line):
                assert allowed.search(line), (step.name, line)


def test_verify_script_covers_the_required_checks() -> None:
    text = access.verify_script()
    assert text.count("--check-only") == 1 and "script:dango-run nb:dango-nb" in text
    assert "sudo -n -l" in text and "sudo -n -l -U" in text
    for group in rs.FORBIDDEN_GROUPS:
        assert group in text
    assert "999|61000|61001" in text and "61000 61001" in text
    assert "setpriv --reuid=$m" in text and "for m in 61000 999" in text
    assert "setfacl -m u:dango-nb:r" in text
    assert text.rstrip().endswith('exit "$bad"')


def test_project_script_denies_the_documented_paths() -> None:
    text = access.project_permission_script()
    for needle in ('"$P/.dango"', '"$P/.dlt"', '"$P/.env"', '"$H"', "chmod 0700", "chmod 0600"):
        assert needle in text
    assert "u:dango-run:---,u:dango-nb:---" in text
    assert "setfacl -m u:dango-nb:--- " in text
    assert "d:u::rwx,d:g::r-x,d:o::---" in text


def test_notebook_acl_text_matches_the_design() -> None:
    assert access.NOTEBOOKS_ACL == ("u:dango-nb:rwx,d:u:dango-nb:rwx,d:u:dango:rwx,d:m::rwx,m::rwx")
    assert "setfacl -P -m u:dango-nb:rw,m::rw" in access.notebooks_acl_script()


def test_scripts_are_stable_across_renders() -> None:
    assert _all_scripts() == _all_scripts()
