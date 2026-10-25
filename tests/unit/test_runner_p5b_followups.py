"""tests/unit/test_runner_p5b_followups.py

Runner follow-ups from the P4b round-4 review: the ssh denial check asks sshd about the real
local address and port and tells "could not evaluate" from "not effective" (L-a), and the
notebook user's loopback range is the host's ephemeral range at install time (L-b).
Shell text runs under dash against temp files and fake tools.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from dango.platform.cloud import runner_persistence as rp
from dango.platform.cloud import runner_scripts as rs
from tests.unit.test_runner_hardening import DASH, Box
from tests.unit.test_runner_review2 import _check_run, _clean_state, _paths

pytestmark = pytest.mark.unit

DEFAULT_RANGE = "32768-60999"


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


# --- L-a: sshd -T with the local address and port ---------------------------------------------


def _sshd(box: Box, *, port_out: str = "port 22", eval_rc: int = 0, denied: bool = True) -> None:
    """Fake sshd: plain ``-T`` prints *port_out*; ``-T -C`` fails (*eval_rc*) or prints the deny list."""
    deny = 'echo "denyusers dango-run"; echo "denyusers dango-nb"' if denied else "echo port 22"
    box.tool(
        "sshd",
        'echo "sshd $*" >>"$LOG"\n'
        'case "$1" in -t) exit 0;; esac\n'
        f'case "$*" in *-C*) [ {eval_rc} -eq 0 ] || {{ echo "Match LocalPort not evaluated" >&2;'
        f" exit {eval_rc}; }}; {deny};; *) echo {port_out!r};; esac\n",
    )


def _setup_box(box: Box) -> Path:
    d = box.root / "sshd_config.d"
    d.mkdir()
    box.tool("id", "echo 1001\n")
    box.tool("chown", "exit 0\n")
    box.tool("systemctl", "exit 0\n")
    return d


def _c_specs(box: Box) -> list[str]:
    return [c for c in box.calls() if c.startswith("sshd -T -C")]


def test_setup_check_names_local_address_and_port() -> None:
    text = rp.ssh_deny_script()
    assert "laddr=127.0.0.1,lport=$SP" in text
    assert "user=$u,host=localhost,addr=127.0.0.1" in text


def test_verify_check_names_local_address_and_port() -> None:
    assert "laddr=127.0.0.1,lport=$SP" in rp.persistence_checks()


def test_setup_uses_the_configured_sshd_port(box: Box) -> None:
    d = _setup_box(box)
    _sshd(box, port_out="port 2222")
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 0, res.stderr
    specs = _c_specs(box)
    assert len(specs) == 2 and all("laddr=127.0.0.1,lport=2222" in s for s in specs)


@pytest.mark.parametrize(
    "out", ["", "garbage", "port 0", "port 70000", "port 99999999999999999999"]
)
def test_unusable_sshd_port_falls_back_to_22(box: Box, out: str) -> None:
    d = _setup_box(box)
    _sshd(box, port_out=out)
    res = box.run(rp.ssh_deny_script(str(d)))
    assert res.returncode == 0, res.stderr
    assert all("lport=22" in s and "lport=220" not in s for s in _c_specs(box))


def test_setup_tells_could_not_evaluate_from_not_effective(box: Box) -> None:
    d = _setup_box(box)
    _sshd(box, eval_rc=255)
    unevaluated = box.run(rp.ssh_deny_script(str(d)))
    _sshd(box, denied=False)
    ineffective = box.run(rp.ssh_deny_script(str(d)))
    assert unevaluated.returncode != 0 and ineffective.returncode != 0
    assert unevaluated.returncode != ineffective.returncode
    assert "could not evaluate" in unevaluated.stderr
    assert "NOT enabled" in unevaluated.stderr and "Include" not in unevaluated.stderr
    assert "Include" in ineffective.stderr and "could not evaluate" not in ineffective.stderr
    assert "Match LocalPort not evaluated" in unevaluated.stderr


def test_verify_tells_could_not_evaluate_from_not_effective(box: Box) -> None:
    etc, _home = _clean_state(box)
    box.tool("sshd", 'case "$*" in *-C*) echo "bad Match" >&2; exit 255;; esac\necho port 22\n')
    unevaluated = _check_run(box, etc, _paths(box))
    box.tool("sshd", 'case "$*" in *-C*) echo "denyusers dango-run";; esac\n')
    ineffective = _check_run(box, etc, _paths(box))
    assert unevaluated.returncode == 1 and ineffective.returncode == 1
    assert "could not evaluate" in unevaluated.stderr
    assert "does not deny" in ineffective.stderr and "could not evaluate" not in ineffective.stderr


def test_verify_passes_when_sshd_denies_with_port_probe(box: Box) -> None:
    etc, _home = _clean_state(box)
    _sshd(box, port_out="port 2222")
    res = _check_run(box, etc, _paths(box))
    assert res.returncode == 0, res.stderr
    assert any("lport=2222" in s for s in _c_specs(box))


def test_ssh_scripts_pass_syntax_check() -> None:
    for text in (rp.ssh_deny_script(), rp.persistence_checks(), rp.persistence_script()):
        res = subprocess.run([DASH, "-n", "-c", text], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr


# --- L-b: the ephemeral range is read at install time ------------------------------------------


def _range(tmp_path: Path, content: bytes | None) -> tuple[str, str]:
    """Run the range fragment against a temp file (or none); returns (EP, stderr)."""
    path = tmp_path / "ip_local_port_range"
    if content is not None:
        path.write_bytes(content)
    res = subprocess.run(
        [DASH, "-c", rs.ephemeral_range_script(str(path)) + 'printf %s "$EP"'],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout, res.stderr


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b"32768\t60999\n", "32768-60999"),
        (b"10000 65000\n", "10000-65000"),
        (b"  15000\t\t40000  \n", "15000-40000"),
        (b"1024 65535\n", "1024-65535"),
        (b"20000\t30000", "20000-30000"),
    ],
)
def test_valid_range_is_used_without_a_warning(
    tmp_path: Path, content: bytes, expected: str
) -> None:
    ep, err = _range(tmp_path, content)
    assert ep == expected and err == ""


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"",
        b"\n",
        b"garbage\n",
        b"32768\n",
        b"abc def\n",
        b"1000 60999\n",
        b"32768 65536\n",
        b"60999 32768\n",
        b"0 60999\n",
        b"-1 60999\n",
        b"32768 60999 7\n",
        b"032768 60999\n",
        b"99999999999999999999 99999999999999999999\n",
        b"32768\n60999\n",
        b"$(echo 1) 60999\n",
        b"32768;touch x 60999\n",
    ],
)
def test_unusable_range_falls_back_with_a_warning(tmp_path: Path, content: bytes | None) -> None:
    ep, err = _range(tmp_path, content)
    assert ep == DEFAULT_RANGE
    assert "ephemeral" in err and DEFAULT_RANGE in err
    assert not (tmp_path / "x").exists()


def test_ruleset_renders_a_custom_range_for_the_notebook_user_only() -> None:
    text = rs.nft_ruleset("10000-65000")
    line = 'meta skuid "dango-nb" ip daddr 127.0.0.1 tcp dport 10000-65000 accept'
    assert line in [ln.strip() for ln in text.splitlines()]
    assert (
        f"tcp dport {DEFAULT_RANGE}" not in text
        and 'dango-run" ip daddr 127.0.0.1 tcp dport 10000' not in text
    )


def test_default_ruleset_is_the_documented_default() -> None:
    assert rs.NFT_RULESET == rs.nft_ruleset(DEFAULT_RANGE)
    assert f"tcp dport {DEFAULT_RANGE} accept" in rs.NFT_RULESET


def test_ruleset_comment_says_the_range_is_read_at_install_time() -> None:
    comment = " ".join(rs.NFT_RULESET.split())
    assert "host's ephemeral range at install time" in comment


def test_installer_reads_the_range_before_writing_the_rules() -> None:
    text = rs.nft_install_script()
    assert text.index("/proc/sys/net/ipv4/ip_local_port_range") < text.index("cat >")
    assert "tcp dport ${EP} accept" in text
    assert text.count("DANGO_RUNNER_NFT_EOF") == 2
    assert "<<'DANGO_RUNNER_NFT_EOF'" not in text


def test_installer_ruleset_text_has_no_shell_expansion_but_the_range() -> None:
    body = rs.nft_ruleset("${EP}")
    assert re.findall(r"[$`\\]", body.replace("${EP}", "")) == []


def test_installer_runs_and_renders_the_hosts_range(box: Box, tmp_path: Path) -> None:
    rng = tmp_path / "range"
    rng.write_text("11000\t52000\n")
    out = tmp_path / "rules"
    script = (
        rs.ephemeral_range_script(str(rng))
        + f"cat >{out} <<DANGO_X\n{rs.nft_ruleset('${EP}')}DANGO_X\n"
    )
    res = subprocess.run([DASH, "-c", script], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    assert "tcp dport 11000-52000 accept" in out.read_text()
    assert "${EP}" not in out.read_text()


def test_range_scripts_pass_syntax_check() -> None:
    for text in (rs.ephemeral_range_script(), rs.nft_install_script()):
        res = subprocess.run([DASH, "-n", "-c", text], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
