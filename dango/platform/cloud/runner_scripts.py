"""dango/platform/cloud/runner_scripts.py

Pure shell-text builders for the restricted runner users (users, directories, sudoers,
loopback firewall, marker).  Nothing here runs a command or touches a file, and nothing
calls these builders yet: the server-side wiring is a later change.

Every root script is POSIX ``sh`` and touches only ``<srv>/runner/*``, users, sudoers,
nftables and the marker.  Paths the service account can write (the project, its home) are
handled by :mod:`dango.platform.cloud.runner_access_scripts` and run as that account, so
root never follows a link the service account controls.  Every builder takes ``srv_dir`` so
tests can run the text against a temp tree.
"""

from __future__ import annotations

import shlex

from dango.platform.run_as import KILL, PYTHON, RESERVED_UIDS

SRV_DIR = "/srv/dango"
SERVICE_USER = "dango"
RUN_USER = "dango-run"
NB_USER = "dango-nb"
RUNNER_USERS = (RUN_USER, NB_USER)
#: Groups a runner must never be in (any of them would undo the isolation).
FORBIDDEN_GROUPS = ("docker", "dango", "adm", "sudo", "systemd-journal")
NOLOGIN = "/usr/sbin/nologin"
UID_MIN = 1000
UID_MAX = 60000
#: Ids a runner may not have; ``RESERVED_UIDS`` is the single source (run_as).
RESERVED_IDS = tuple(sorted(RESERVED_UIDS))

SUDOERS_PATH = "/etc/sudoers.d/dango-runner"
NFT_DIR = "/etc/nftables.d"
NFT_PATH = f"{NFT_DIR}/dango-runner.nft"
NFT_TABLE = "dango_runner"
NFT_BIN = "/usr/sbin/nft"
UNIT_NAME = "dango-runner-net.service"
UNIT_PATH = f"/etc/systemd/system/{UNIT_NAME}"
#: Schema version written into the marker record.
MARKER_SCHEMA = 1
LOOPBACK_RESTRICTED = "restricted"
LOOPBACK_UNRESTRICTED = "unrestricted"
#: What the marker's ``restricted`` means: the runners cannot open connections to loopback, to any
#: address of this host, or to the Docker bridge networks (containers), except DNS and the
#: notebook's own port.
LOOPBACK_DOC = (
    "restricted: runner users cannot connect to loopback, this host's own addresses or the "
    "Docker bridge networks (DNS and the notebook port excepted); unrestricted: no firewall rules"
)

SUDOERS_TEXT = (
    "# Installed by Dango. The service account may run code as the restricted runner users,\n"
    "# never as root; the runner users get no sudo rule of their own.\n"
    "Defaults:dango !requiretty\n"
    "Defaults:dango !use_pty\n"
    f"Cmnd_Alias DANGO_RUNNER = {PYTHON} -I -m dango.runtime.isolated_run *\n"
    f"Cmnd_Alias DANGO_SIGNAL = {KILL}\n"
    f"dango ALL=({RUN_USER},{NB_USER}) NOPASSWD:SETENV: DANGO_RUNNER, DANGO_SIGNAL\n"
)

#: ``net.ipv4.ip_local_port_range`` default: where a notebook kernel's loopback listener lives.
EPHEMERAL_PORTS = "32768-60999"
PORT_RANGE_FILE = "/proc/sys/net/ipv4/ip_local_port_range"


def nft_ruleset(ports: str = EPHEMERAL_PORTS) -> str:
    """The loopback ruleset; *ports* (``A-B``) is the notebook user's ephemeral loopback range."""
    return f"""# Installed by Dango: runner users cannot connect to local services, this host or Docker networks.
add table inet {NFT_TABLE}
delete table inet {NFT_TABLE}
table inet {NFT_TABLE} {{
  chain output {{
    type filter hook output priority 0; policy accept;
    ct state established,related accept
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} ip daddr 127.0.0.0/8 udp dport 53 accept
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} ip daddr 127.0.0.0/8 tcp dport 53 accept
    meta skuid "{NB_USER}" ip daddr 127.0.0.1 tcp dport 7805 accept
    # Dango's own services use fixed low ports (80, 443, 3000, 7805, 8080, 8081, 8800), outside the
    # default ephemeral range.  The notebook server's kernel connects back over loopback on an
    # ephemeral port, so dango-nb (never dango-run) may reach loopback in that range; the range is
    # the host's ephemeral range at install time (the default 32768-60999 when it cannot be read).
    meta skuid "{NB_USER}" ip daddr 127.0.0.1 tcp dport {ports} accept
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} oifname "docker0" reject
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} oifname "br-*" reject
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} fib daddr type local reject
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} ip daddr 127.0.0.0/8 reject
    meta skuid {{ "{RUN_USER}", "{NB_USER}" }} ip6 daddr ::1 reject
  }}
}}
"""


NFT_RULESET = nft_ruleset()


def ephemeral_range_script(path: str = PORT_RANGE_FILE) -> str:
    """Shell lines (POSIX ``sh``) that set ``EP`` to the host's ephemeral port range, ``A-B``.

    Two plain integers in 1024-65535 with ``A <= B`` are used as read; anything else (missing
    file, garbage, leading zeros, extra fields, out of bounds) gives the default with a warning
    on stderr.  The values are only ever digits when they reach the ruleset.
    """
    default = EPHEMERAL_PORTS
    return (
        f"EP={default}; lo=; hi=; extra=\n"
        f"[ ! -r {q(path)} ] || read -r lo hi extra <{q(path)} || true\n"
        'ok=1; for v in "$lo" "$hi"; do case "$v" in\n'
        "  ''|0*|*[!0-9]*|??????*) ok=0;; esac; done\n"
        '[ -z "$extra" ] || ok=0\n'
        '[ "$ok" = 1 ] && [ "$lo" -ge 1024 ] && [ "$hi" -le 65535 ] && [ "$lo" -le "$hi" ] || ok=0\n'
        'if [ "$ok" = 1 ]; then EP="$lo-$hi"; else\n'
        f'  echo "warning: could not read a valid ephemeral port range from {path}; using {default}"'
        " >&2\nfi\n"
    )


NFT_UNIT = f"""[Unit]
Description=Dango runner loopback rules
DefaultDependencies=no
Wants=network-pre.target
Before=network-pre.target
Before=network.target
Conflicts=shutdown.target
Before=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={NFT_BIN} -f {NFT_PATH}
ExecStop=-{NFT_BIN} delete table inet {NFT_TABLE}

[Install]
WantedBy=multi-user.target
"""

_HEAD = "set -eu\n"

#: Drops empty and relative ``PATH`` entries: GNU ``find -execdir`` refuses to run when it finds one.
SAFE_PATH = (
    '_np=; IFS=:; for _d in $PATH; do case "$_d" in /*) _np="${_np:+$_np:}$_d";; esac; done; '
    'unset IFS; PATH="$_np"; export PATH\n'
)


def q(value: str) -> str:
    """Shell-quote *value*."""
    return shlex.quote(value)


def as_service_user(script: str) -> str:
    """Command that runs *script* as the service account (never root) via ``runuser``."""
    return f"runuser -u {SERVICE_USER} -- sh -c {q(script)}"


def runner_dir(srv_dir: str = SRV_DIR) -> str:
    """Root-owned parent of the runner homes and the marker."""
    return f"{srv_dir}/runner"


def marker_path(srv_dir: str = SRV_DIR) -> str:
    """Path of the root-owned marker that switches isolation on."""
    return f"{runner_dir(srv_dir)}/active"


def _homes(srv_dir: str) -> dict[str, str]:
    base = runner_dir(srv_dir)
    return {RUN_USER: f"{base}/scripts", NB_USER: f"{base}/nb-home"}


def _heredoc(tmp: str, text: str, tag: str, *, expand: bool = False) -> str:
    if tag in text:
        raise ValueError("heredoc tag appears in the text")
    return f"cat >{q(tmp)} <<{tag if expand else repr(tag)}\n{text}{tag}\n"


def _guard(paths: list[str]) -> str:
    quoted = " ".join(q(p) for p in paths)
    return (
        f"for p in {quoted}; do "
        'if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi; done\n'
    )


def package_script(package: str, tool: str) -> str:
    """Install *package* when *tool* is missing (lists refreshed first); exits 6 if still absent."""
    lock = "-o DPkg::Lock::Timeout=120"
    return (
        _HEAD
        + "export DEBIAN_FRONTEND=noninteractive\n"
        + f"command -v {q(tool)} >/dev/null 2>&1 || {{ apt-get update -qq {lock}"
        + f" && apt-get install -y -qq {q(package)} {lock}; }}\n"
        + f"command -v {q(tool)} >/dev/null 2>&1"
        + f" || {{ echo {q(f'{tool} is not available')} >&2; exit 6; }}\n"
    )


def runner_base_dir_script(srv_dir: str = SRV_DIR) -> str:
    """Create ``<srv>/runner`` (root:root 0755); refuses a symlink at any managed path."""
    base = runner_dir(srv_dir)
    homes = _homes(srv_dir)
    paths = [base, *homes.values(), f"{base}/snapshots", marker_path(srv_dir)]
    return (
        _HEAD
        + _guard(paths)
        + f"install -d -o root -g root -m 0755 {q(base)}\n"
        + f"chown root:root {q(base)}\n"
        + f"chmod 0755 {q(base)}\n"
    )


def users_script(srv_dir: str = SRV_DIR) -> str:
    """Create both runner users (no login, locked password) and assert the id rules.

    Exits 5 when an id is reserved or outside 1000-59999, when the two users share an id,
    or when an existing user has a login shell.
    """
    homes = _homes(srv_dir)
    reserved = "|".join(str(i) for i in RESERVED_IDS)
    lines = [_HEAD.rstrip("\n")]
    lines.append(
        'bad() { echo "runner user check failed: $*" >&2; exit 5; }\n'
        'chk() { for v in "$(id -u "$1")" "$(id -g "$1")"; do\n'
        f'    case "$v" in {reserved}) bad "$1 has reserved id $v";; esac\n'
        f'    [ "$v" -ge {UID_MIN} ] && [ "$v" -lt {UID_MAX} ] || bad "$1 id $v out of range"\n'
        "  done; }"
    )
    for user, home in homes.items():
        lines.append(
            f"getent passwd {q(user)} >/dev/null 2>&1 || useradd --no-create-home"
            f" --home-dir {q(home)} --shell {NOLOGIN} --user-group {q(user)}"
        )
        lines.append(
            f'[ "$(getent passwd {q(user)} | cut -d: -f7)" = {NOLOGIN} ] || bad {q(user + " has a login shell")}'
        )
        lines.append(f"passwd -l {q(user)} >/dev/null")
        lines.append(f"chk {q(user)}")
    lines.append(
        f'[ "$(id -u {RUN_USER})" != "$(id -u {NB_USER})" ] || bad \'runner users share a uid\''
    )
    return "\n".join(lines) + "\n"


def runner_dirs_script(srv_dir: str = SRV_DIR) -> str:
    """Runner homes and the snapshot directory, as root, without recursion.

    Only these exact directories are touched (never their contents).  ``snapshots`` is
    ``dango:dango-nb`` 2750: the setgid bit gives new snapshot files group ``dango-nb``, and
    the service account, as owner, can delete them.
    """
    base = runner_dir(srv_dir)
    homes = _homes(srv_dir)
    snap = f"{base}/snapshots"
    lines = [_HEAD + _guard([base, *homes.values(), snap]).rstrip("\n")]
    for user, home in homes.items():
        lines.append(f"install -d -o {user} -g {user} -m 0700 {q(home)}")
        lines.append(f"chown {user}:{user} {q(home)}")
        lines.append(f"chmod 0700 {q(home)}")
    lines.append(f"install -d -o {SERVICE_USER} -g {NB_USER} -m 0750 {q(snap)}")
    lines.append(f"chown {SERVICE_USER}:{NB_USER} {q(snap)}")
    lines.append(f"chmod 2750 {q(snap)}")
    return "\n".join(lines) + "\n"


def sudoers_install_script() -> str:
    """Install the sudoers drop-in: temp file, 0440, ``visudo -cf``, move, ``visudo -c``, rule check.

    The final ``sudo -n -l -U dango`` check catches hosts whose sudoers lacks ``@includedir``;
    a failure removes the file again, so a rejected rule is never left behind.
    """
    tmp = f"{SUDOERS_PATH}.tmp"
    fail = "echo 'sudoers check failed' >&2; exit 4"
    return (
        _HEAD
        + _guard([SUDOERS_PATH, tmp])
        + "install -d -o root -g root -m 0750 /etc/sudoers.d\n"
        + _heredoc(tmp, SUDOERS_TEXT, "DANGO_RUNNER_SUDOERS_EOF")
        + f"chown root:root {tmp}\nchmod 0440 {tmp}\n"
        + f"visudo -cf {tmp} >/dev/null || {{ rm -f {tmp}; {fail}; }}\n"
        + f"mv -f {tmp} {SUDOERS_PATH}\n"
        + f"visudo -c >/dev/null || {{ rm -f {SUDOERS_PATH}; {fail}; }}\n"
        + "sudo -n -l -U dango 2>/dev/null | grep -qF dango.runtime.isolated_run"
        + f" || {{ rm -f {SUDOERS_PATH}; echo 'sudoers rule not active' >&2; exit 4; }}\n"
    )


def nft_install_script() -> str:
    """Install the loopback rules and load them through their own oneshot unit.

    Best effort: any failure exits non-zero and the caller records ``loopback: unrestricted``.
    The system ``nftables`` configuration and the host firewall are never edited.
    """
    tmp_rules = f"{NFT_PATH}.tmp"
    tmp_unit = f"{UNIT_PATH}.tmp"
    return (
        _HEAD
        + f"[ -x {NFT_BIN} ] || {{ echo 'nft is not installed' >&2; exit 6; }}\n"
        + _guard([NFT_DIR, NFT_PATH, tmp_rules, UNIT_PATH, tmp_unit])
        + f"install -d -o root -g root -m 0755 {NFT_DIR}\n"
        + ephemeral_range_script()
        # unquoted tag: only ``${EP}`` (digits and one dash, validated above) is expanded
        + _heredoc(tmp_rules, nft_ruleset("${EP}"), "DANGO_RUNNER_NFT_EOF", expand=True)
        + f"chown root:root {tmp_rules}\nchmod 0644 {tmp_rules}\n"
        + f"{NFT_BIN} -c -f {tmp_rules} || {{ rm -f {tmp_rules}; echo 'nft rules rejected' >&2; exit 6; }}\n"
        + f"mv -f {tmp_rules} {NFT_PATH}\n"
        + _heredoc(tmp_unit, NFT_UNIT, "DANGO_RUNNER_UNIT_EOF")
        + f"chown root:root {tmp_unit}\nchmod 0644 {tmp_unit}\nmv -f {tmp_unit} {UNIT_PATH}\n"
        + "systemctl daemon-reload\n"
        + f"systemctl enable {UNIT_NAME} >/dev/null 2>&1\n"
        + f"systemctl restart {UNIT_NAME}\n"
        + f"{NFT_BIN} list table inet {NFT_TABLE} >/dev/null\n"
    )


def marker_script(srv_dir: str = SRV_DIR) -> str:
    """Write the marker LAST: a root-owned regular file nobody else can write.

    The record states whether the loopback table is actually loaded, read at write time.
    """
    base = runner_dir(srv_dir)
    marker = marker_path(srv_dir)
    tmp = f"{base}/.active.tmp"
    return (
        _HEAD
        + _guard([base, marker])
        + f"[ ! -d {q(marker)} ] || {{ echo 'marker path is a directory' >&2; exit 3; }}\n"
        + f"if nft list table inet {NFT_TABLE} >/dev/null 2>&1; then L={LOOPBACK_RESTRICTED};"
        + f" else L={LOOPBACK_UNRESTRICTED}; fi\n"
        + f"rm -f {q(tmp)}\numask 022\n"
        + f'printf \'{{"schema": {MARKER_SCHEMA}, "loopback": "%s"}}\\n\' "$L" >{q(tmp)}\n'
        + f"chown root:root {q(tmp)}\nchmod 0644 {q(tmp)}\nmv -f {q(tmp)} {q(marker)}\n"
    )


def disable_script(srv_dir: str = SRV_DIR) -> str:
    """Turn isolation off: remove the marker only (users, files and rules stay)."""
    return _HEAD + f"rm -f {q(marker_path(srv_dir))}\n"


def remove_script(srv_dir: str = SRV_DIR) -> str:
    """Disable, then remove the sudoers rule, the rules file and the unit (users stay)."""
    return (
        disable_script(srv_dir)
        + f"systemctl disable --now {UNIT_NAME} >/dev/null 2>&1 || true\n"
        + f"{NFT_BIN} delete table inet {NFT_TABLE} >/dev/null 2>&1 || true\n"
        + f"rm -f {NFT_PATH} {UNIT_PATH} {SUDOERS_PATH}\n"
        + "systemctl daemon-reload >/dev/null 2>&1 || true\n"
    )
