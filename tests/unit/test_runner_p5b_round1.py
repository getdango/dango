"""tests/unit/test_runner_p5b_round1.py

Round-1 review fixes for C6-P5b: the notebook user's ephemeral loopback range never opens Dango's
fixed ports (M-1), a one-port range renders valid nft (L-1), the range is read with a method that
works under dash and bash on /proc (L-2), every sshd port is checked (L-3), and a concurrent live
run of the same isolation command is neither removed nor mistaken for a leftover (L-4).
Shell text runs under dash against temp files and fake tools.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dango.config.models import PlatformSettings
from dango.platform.cloud import runner_persistence as rp
from dango.platform.cloud import runner_scripts as rs
from dango.platform.cloud._server_templates import CADDYFILE
from tests.factories.remote_isolation_fakes import IsoSSH, invoke
from tests.unit.test_p4c_lows import OWN_DISABLE, PREFIX, _plain
from tests.unit.test_runner_hardening import DASH, Box
from tests.unit.test_runner_p5b_followups import _range
from tests.unit.test_runner_review2 import _check_run, _clean_state, _paths

pytestmark = pytest.mark.unit

EXCLUDED = "{ 80, 443, 2019, 3000, 8080, 8081, 8800 }"
ACCEPT = 'meta skuid "dango-nb" ip daddr 127.0.0.1 tcp dport PORTS tcp dport != ' + EXCLUDED
ACCEPT += " accept"


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _accept(ports: str) -> str:
    return ACCEPT.replace("PORTS", ports)


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines()]


# --- M-1: the fixed Dango ports are carved out of the ephemeral allow rule -----------------------


def test_default_ruleset_excludes_the_fixed_ports() -> None:
    assert _accept("32768-60999") in _lines(rs.NFT_RULESET)


def test_overlapping_range_still_excludes_the_fixed_ports() -> None:
    for ports in ("1024-65535", "3000-9000", "40000"):
        assert _accept(ports) in _lines(rs.nft_ruleset(ports))


def test_only_the_notebook_user_gets_an_ephemeral_rule() -> None:
    rules = [ln for ln in _lines(rs.NFT_RULESET) if "tcp dport !=" in ln]
    assert len(rules) == 1 and '"dango-nb"' in rules[0] and "dango-run" not in rules[0]


def test_installer_template_keeps_the_exclusion() -> None:
    assert _accept("${EP}") in _lines(rs.nft_install_script())


def test_ruleset_comment_describes_the_exclusion() -> None:
    comment = " ".join(rs.NFT_RULESET.split())
    assert "except Dango's fixed ports" in comment


def _fixed_ports() -> set[int]:
    return set(rs.FIXED_LOOPBACK_PORTS)


def test_every_fixed_dango_port_in_the_code_is_excluded() -> None:
    settings = PlatformSettings()
    configured = {
        settings.port,
        settings.metabase_port,
        settings.dbt_docs_port,
    }
    caddy = {int(p) for p in re.findall(r"localhost:(\d+)", CADDYFILE)}
    caddy |= {int(p) for p in re.findall(r"^:(\d+) \{", CADDYFILE, flags=re.M)}
    caddy.discard(settings.marimo_port)  # websocket proxy; has its own accept rule
    assert configured <= _fixed_ports()
    assert caddy and caddy <= _fixed_ports()
    # Caddy's admin API default, https, the dev server; the notebook port has its own rule
    assert {443, 2019, 8080} <= _fixed_ports()
    assert settings.marimo_port not in _fixed_ports()


def test_the_exclusion_set_is_rendered_from_the_constant() -> None:
    rendered = "{ " + ", ".join(str(p) for p in sorted(_fixed_ports())) + " }"
    assert rendered == EXCLUDED


# --- L-1: low == high renders a single port ---------------------------------------------------------


def test_one_port_range_renders_a_single_port(tmp_path: Path) -> None:
    ep, err = _range(tmp_path, b"40000\t40000\n")
    assert ep == "40000" and err == ""


def test_two_port_range_still_renders_a_dash(tmp_path: Path) -> None:
    ep, err = _range(tmp_path, b"40000\t40001\n")
    assert ep == "40000-40001" and err == ""


# --- L-2: the range is read without a method that dash misreads on /proc --------------------------


def test_range_fragment_does_not_read_the_file_with_read() -> None:
    text = rs.ephemeral_range_script()
    assert "$(cat " in text
    assert not re.search(r"read [^\n]*<\s*'?/proc", text)
    assert not re.search(r"read [^\n]*<\s*'?" + re.escape(rs.PORT_RANGE_FILE), text)


def test_range_fragment_reads_the_real_proc_file_under_dash(tmp_path: Path) -> None:
    proc = Path(rs.PORT_RANGE_FILE)
    ep, err = _range(tmp_path, None)  # a missing file still falls back
    assert ep == "32768-60999" and err
    if proc.exists():
        res = subprocess.run(
            [DASH, "-c", rs.ephemeral_range_script() + 'printf %s "$EP"'],
            capture_output=True,
            text=True,
        )
        lo, hi = proc.read_text().split()
        want = lo if lo == hi else f"{lo}-{hi}"
        assert res.stdout == want and res.stderr == ""


def test_glob_characters_in_the_range_file_are_not_expanded(tmp_path: Path) -> None:
    (tmp_path / "1").write_text("")
    ep, err = _range(tmp_path, b"* *\n")
    assert ep == "32768-60999" and "ephemeral" in err


# --- L-3: every sshd port is checked --------------------------------------------------------------------


def _multi_port_sshd(box: Box, ports: tuple[int, ...], deny_on: tuple[int, ...]) -> None:
    """Fake sshd listening on *ports*; denies the runners only for a spec naming a *deny_on* port."""
    out = "; ".join(f"echo 'listenaddress 0.0.0.0:{p}'" for p in ports)
    cases = "\n".join(
        f'  *"lport={p}") echo "denyusers dango-run"; echo "denyusers dango-nb";;' for p in deny_on
    )
    box.tool(
        "sshd",
        'echo "sshd $*" >>"$LOG"\ncase "$1" in -t) exit 0;; esac\n'
        f'case "$*" in *user=root*) {out};; *-C*) case "$*" in\n{cases}\n  *) echo "port 22";; esac;;\n'
        "*) exit 255;; esac\n",
    )


def _setup(box: Box) -> Path:
    d = box.root / "sshd_config.d"
    d.mkdir()
    box.tool("id", "echo 1001\n")
    box.tool("chown", "exit 0\n")
    box.tool("systemctl", "exit 0\n")
    return d


def _specs(box: Box) -> list[str]:
    return [c for c in box.calls() if c.startswith("sshd -T -C") and "user=root" not in c]


def test_setup_checks_every_port_for_both_users(box: Box) -> None:
    d = _setup(box)
    _multi_port_sshd(box, (22, 2222), (22, 2222))
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 0, res.stderr
    specs = _specs(box)
    assert len(specs) == 4
    assert sum("lport=22 " in s + " " for s in specs) == 2
    assert sum("lport=2222" in s for s in specs) == 2


def test_setup_fails_when_a_later_port_does_not_deny(box: Box) -> None:
    d = _setup(box)
    _multi_port_sshd(box, (22, 2222), (22,))
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 6 and "NOT enabled" in res.stderr


def test_verify_checks_every_port(box: Box) -> None:
    etc, _home = _clean_state(box)
    _multi_port_sshd(box, (22, 2222), (22, 2222))
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 0, res.stderr
    assert any("lport=2222" in s for s in _specs(box)) and any(
        "lport=22 " in s + " " for s in _specs(box)
    )


def test_verify_fails_when_a_later_port_does_not_deny(box: Box) -> None:
    etc, _home = _clean_state(box)
    _multi_port_sshd(box, (22, 2222), (22,))
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 1 and "does not deny" in res.stderr


def test_duplicate_ports_are_checked_once(box: Box) -> None:
    d = _setup(box)
    _multi_port_sshd(box, (22, 22, 2222), (22, 2222))
    assert box.run(rp.ssh_deny_script(str(d))).returncode == 0
    assert len(_specs(box)) == 4


# --- L-4: a live concurrent run is not a leftover -------------------------------------------------------

LIVE = f"{PREFIX}-isolation-disable-20261025T000000Z-ab12cd.timer"


def _disable(ssh: IsoSSH) -> Any:
    return invoke(ssh, ["disable", "--yes"])


def test_a_recently_armed_guard_of_the_same_command_blocks_and_is_kept() -> None:
    ssh = IsoSSH(guard_units=[LIVE], unit_ages={LIVE: 20})
    result = _disable(ssh)
    out = _plain(result.output)
    assert result.exit_code == 1 and "dango remote isolation disable" in out
    assert "leftover" not in out.lower() and LIVE.removesuffix(".timer") in out
    assert "guard:arm" not in ssh.events and "guard:disarm" not in ssh.events
    assert LIVE in ssh.units and "disable_marker" not in ssh.events


def test_a_stale_guard_of_the_same_command_is_cleaned_and_reported() -> None:
    ssh = IsoSSH(guard_units=[OWN_DISABLE], unit_ages={OWN_DISABLE: 3600})
    result = _disable(ssh)
    assert result.exit_code == 0, result.output
    assert "Removed a leftover guard" in _plain(result.output)
    assert OWN_DISABLE not in ssh.units


def test_a_guard_whose_age_is_unknown_is_not_removed() -> None:
    ssh = IsoSSH(guard_units=[LIVE], unit_ages={LIVE: None})
    result = _disable(ssh)
    assert result.exit_code == 1 and LIVE in ssh.units and "guard:disarm" not in ssh.events


def test_no_leftover_message_when_there_was_no_leftover() -> None:
    result = _disable(IsoSSH())
    assert result.exit_code == 0 and "leftover" not in _plain(result.output).lower()


def test_each_run_gets_a_unique_owner_tag() -> None:
    from dango.cli.commands.remote_isolation import _OwnedGuard

    a = _OwnedGuard(IsoSSH(), [], "isolation-disable")
    b = _OwnedGuard(IsoSSH(), [], "isolation-disable")
    assert a.tag != b.tag and a.tag.startswith("isolation-disable-")
    assert a.tag in a._run_id
