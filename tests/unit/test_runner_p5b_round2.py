"""tests/unit/test_runner_p5b_round2.py

Round-2 review fixes for C6-P5b: the sshd port list comes from the ``listenaddress`` lines of
``sshd -T -C`` and fails closed when that cannot be evaluated, because OpenSSH 8.9's plain
``sshd -T`` exits 255 once a ``Match LocalPort`` block exists (L-3); a live same-command run's
guard is treated as live for the whole guard window (L-4); the loopback range comment records that
other local services inside the host's range are reachable (L-5).  Shell runs under dash.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.commands import remote_isolation as ri
from dango.platform.cloud import runner_persistence as rp
from dango.platform.cloud import runner_scripts as rs
from dango.platform.cloud.upgrade_guard import GUARD_WINDOW
from tests.factories.remote_isolation_fakes import IsoSSH, invoke
from tests.unit.test_p4c_lows import PREFIX, _plain
from tests.unit.test_runner_hardening import Box
from tests.unit.test_runner_review2 import _check_run, _clean_state, _paths

pytestmark = pytest.mark.unit


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _sshd89(box: Box, listen: tuple[str, ...], *, deny_on: tuple[int, ...] = (22,), rc: int = 0):
    """Fake OpenSSH 8.9: ``-T`` without ``-C`` exits 255; the port probe prints *listen*.

    The per-user spec denies the runners only for an ``lport`` in *deny_on* (a Match LocalPort).
    """
    probe = "; ".join(f"echo 'listenaddress {a}'" for a in listen)
    cases = "\n".join(
        f'  *"lport={p}") echo "denyusers dango-run"; echo "denyusers dango-nb";;' for p in deny_on
    )
    box.tool(
        "sshd",
        'echo "sshd $*" >>"$LOG"\ncase "$1" in -t) exit 0;; esac\n'
        'case "$*" in *-C*) ;; *) echo "Match LocalPort in config but lport not given" >&2;'
        " exit 255;; esac\n"
        f'[ {rc} -eq 0 ] || {{ echo "cannot evaluate" >&2; exit {rc}; }}\n'
        f'case "$*" in *user=root*) {probe};; *) case "$*" in\n{cases}\n  *) echo "port 22";;'
        " esac;; esac\n",
    )


def _setup(box: Box) -> Path:
    d = box.root / "sshd_config.d"
    d.mkdir()
    box.tool("id", "echo 1001\n")
    box.tool("chown", "exit 0\n")
    box.tool("systemctl", "exit 0\n")
    return d


def _lports(box: Box) -> list[str]:
    calls = [c for c in box.calls() if c.startswith("sshd -T -C") and "user=root" not in c]
    return sorted({c.rsplit("lport=", 1)[1] for c in calls})


# --- L-3: ports come from `sshd -T -C`, never from a plain `sshd -T` ---------------------------


def test_port_list_is_read_with_a_connection_spec() -> None:
    for text in (rp.ssh_deny_script(), rp.persistence_checks()):
        assert "listenaddress" in text and '"$SSHD" -T 2>' not in text
        assert (
            '"$SSHD" -T -C "user=root,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22"'
            in text
        )


def test_match_localport_block_ports_are_all_checked(box: Box) -> None:
    d = _setup(box)
    _sshd89(box, ("0.0.0.0:22", "0.0.0.0:2222"), deny_on=(22, 2222))
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 0, res.stderr
    assert _lports(box) == ["22", "2222"]


def test_match_localport_port_that_does_not_deny_fails_setup(box: Box) -> None:
    d = _setup(box)
    _sshd89(box, ("0.0.0.0:22", "0.0.0.0:2222"), deny_on=(22,))
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 6 and "2222" in res.stderr


def test_listenaddress_host_and_port_forms(box: Box) -> None:
    d = _setup(box)
    _sshd89(box, ("0.0.0.0:2223", "[::]:2224", "127.0.0.1:22"), deny_on=(22, 2223, 2224))
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 0, res.stderr
    assert _lports(box) == ["22", "2223", "2224"]


def test_two_ports_and_a_duplicate_are_checked_once_each(box: Box) -> None:
    d = _setup(box)
    _sshd89(box, ("0.0.0.0:22", "[::]:22", "0.0.0.0:2222"), deny_on=(22, 2222))
    assert box.run(rp.ssh_deny_script(str(d))).returncode == 0
    assert _lports(box) == ["22", "2222"]


def test_setup_fails_closed_when_the_port_probe_fails(box: Box) -> None:
    d = _setup(box)
    _sshd89(box, ("0.0.0.0:22",), rc=255)
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 7 and "could not evaluate" in res.stderr
    assert "NOT enabled" in res.stderr


def test_verify_checks_match_localport_ports(box: Box) -> None:
    etc, _home = _clean_state(box)
    _sshd89(box, ("0.0.0.0:22", "0.0.0.0:2222"), deny_on=(22,))
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 1 and "does not deny" in res.stderr and "2222" in res.stderr


def test_verify_passes_with_every_port_denied(box: Box) -> None:
    etc, _home = _clean_state(box)
    _sshd89(box, ("0.0.0.0:22", "[::]:2222"), deny_on=(22, 2222))
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 0, res.stderr


def test_verify_fails_closed_when_the_port_probe_fails(box: Box) -> None:
    etc, _home = _clean_state(box)
    _sshd89(box, ("0.0.0.0:22",), rc=255)
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 1 and "could not evaluate" in res.stderr


# --- L-4: the live window is the whole guard window ---------------------------------------------


def test_live_window_is_the_guard_window() -> None:
    assert GUARD_WINDOW.endswith("m")
    assert ri._LIVE_WITHIN == int(GUARD_WINDOW.removesuffix("m")) * 60


def test_a_guard_older_than_the_old_threshold_but_inside_the_window_is_live() -> None:
    unit = f"{PREFIX}-isolation-enable-20261025T000000Z-ab12cd.timer"
    ssh = IsoSSH(guard_units=[unit], unit_ages={unit: 610})
    result = invoke(ssh, ["enable", "--yes"])
    out = _plain(result.output)
    assert result.exit_code == 1 and "leftover" not in out.lower()
    assert unit in ssh.units and "guard:disarm" not in ssh.events


def test_busy_message_names_the_guard_window() -> None:
    unit = f"{PREFIX}-isolation-enable-20261025T000000Z-ab12cd.timer"
    ssh = IsoSSH(guard_units=[unit], unit_ages={unit: 20})
    out = _plain(invoke(ssh, ["enable", "--yes"]).output)
    assert "expires within 20 minutes" in out


# --- L-5: the host range is recorded as a trade-off ----------------------------------------------


def test_ruleset_comment_records_the_host_range_trade_off() -> None:
    src = " ".join(Path(rs.__file__).read_text().replace("# ", " ").split())
    assert "other local loopback services" in src
