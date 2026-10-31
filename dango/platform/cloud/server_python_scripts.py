"""dango/platform/cloud/server_python_scripts.py

Shell scripts (POSIX ``sh``) and pinned values for the server venv's interpreter.

Each script runs as root.  A candidate interpreter is judged as data (location, owner and
mode of the file and every directory above it) before it is ever run; the pinned build is
verified (size, SHA-256, archive entries) before it is extracted.  The logic that runs
them over SSH is in :mod:`dango.platform.cloud.server_python`.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass

#: Python versions the venv accepts: equal to ``requires-python`` in ``pyproject.toml``.
MIN_PYTHON = (3, 11)
MAX_PYTHON = (3, 14)  # exclusive
SUPPORTED = "3.11, 3.12 or 3.13"

#: Root-owned home of interpreters Dango installs; one directory per pinned build.
PYTHON_HOME = "/opt/dango/python"
PBS_RELEASE = "20261026"
PYTHON_VERSION = "3.12.16"
PYTHON_TAG = f"cpython-{PYTHON_VERSION}-{PBS_RELEASE}"
PYTHON_DIR = f"{PYTHON_HOME}/{PYTHON_TAG}"
PYTHON_BIN = f"{PYTHON_DIR}/bin/python3.12"
#: File in the installed tree that records the SHA-256 of the archive it came from.
MARKER = ".dango-python.sha256"
DOWNLOAD_HOSTS = "github.com and release-assets.githubusercontent.com"
#: Room above the pinned size for ``curl --max-filesize`` (the exact size is checked after).
SIZE_MARGIN = 1024 * 1024
#: Seconds the install waits for another install's lock, and the download may take.  The
#: script prints nothing until it ends, so together they stay well inside the SSH read
#: timeout (``server_python.INSTALL_STEP_TIMEOUT``).
LOCK_WAIT = 120
DOWNLOAD_MAX_TIME = 600


@dataclass(frozen=True)
class PythonBuild:
    """One pinned ``install_only`` CPython archive."""

    url: str
    size: int
    sha256: str


_RELEASE_URL = (
    f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_RELEASE}/"
)

#: astral-sh/python-build-standalone release 20261026 (published 2026-10-26), CPython 3.12.16,
#: ``install_only`` builds.  Size and SHA-256 were checked against a local download, the
#: release's ``SHA256SUMS`` file and the digest GitHub reports for each release asset.
PYTHON_BUILDS: dict[str, PythonBuild] = {
    "x86_64": PythonBuild(
        url=_RELEASE_URL
        + "cpython-3.12.16%2B20261026-x86_64-unknown-linux-gnu-install_only.tar.gz",
        size=67921566,
        sha256="6a79e922e50006b601f29e688407659da1c2b86ebcd4e9c7254cf234be8b89de",
    ),
    "aarch64": PythonBuild(
        url=_RELEASE_URL
        + "cpython-3.12.16%2B20261026-aarch64-unknown-linux-gnu-install_only.tar.gz",
        size=53051192,
        sha256="a945427411dc6851a1393533b74a4a1ac434321ae397f2ec69897a9b4fde032b",
    ),
}

#: ``uname -m`` values mapped to a key of :data:`PYTHON_BUILDS`.
ARCHES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}

#: Host interpreters tried in order, found on root's ``PATH``.
HOST_NAMES = ("python3.13", "python3.12", "python3.11", "python3")
#: A host interpreter must resolve under here (the venv trust check accepts links into /usr),
#: which also keeps out /srv/dango, home directories and the temporary directories.
HOST_ALLOWED = "/usr"

#: Run by a candidate interpreter (any version, so it must stay valid for old Pythons): the
#: modules a venv and pip need, then ``major.minor.micro.releaselevel``.
PROBE = (
    "import sys, venv, ensurepip, ssl, sqlite3\n"
    "ensurepip.version()\n"
    "v = sys.version_info\n"
    "print('%d.%d.%d.%s' % (v[0], v[1], v[2], v[3]))\n"
)
#: Run only when :data:`PROBE` fails, to name an in-range interpreter that lacks its venv
#: module (Ubuntu 24.04 without ``python3-venv``); such an interpreter is never chosen.
VERSION_PROBE = (
    "import sys\nv = sys.version_info\nprint('%d.%d.%d.%s' % (v[0], v[1], v[2], v[3]))\n"
)

# Exit codes of the install script (mapped to messages in server_python).
EXIT_DOWNLOAD = 10
EXIT_SIZE = 11
EXIT_CHECKSUM = 12
EXIT_ARCHIVE = 13
EXIT_LOCATION = 14
EXIT_TOOLS = 15

# Not root-owned, or group/other-writable (a find predicate for one path at -maxdepth 0).
_NOT_ROOTS = "\\( -type l -o ! -user root -o -perm -020 -o -perm -002 \\)"
# Anything in a tree not root-owned, or group/other-writable (links are judged by their target).
_TREE_UNTRUSTED = "\\( ! -user root -o \\( ! -type l \\( -perm -020 -o -perm -002 \\) \\) \\)"


def accepted_versions() -> list[str]:
    """``case`` patterns for a final release in the accepted range (``3.11.*.final`` ...)."""
    major = MIN_PYTHON[0]
    return [f"{major}.{minor}.*.final" for minor in range(MIN_PYTHON[1], MAX_PYTHON[1])]


def host_python_script(
    *,
    names: Sequence[str] = HOST_NAMES,
    owner: str = "root",
    allowed: str = HOST_ALLOWED,
    top: str = "/",
) -> str:
    """Print the resolved path of the first acceptable host interpreter (nothing if none).

    A candidate is passed over unless its real path is under *allowed*, and the file and
    every directory up to *top* are owned by *owner* and not group/other writable; only then
    does it run :data:`PROBE` (isolated, from ``/``) and report a final release in the
    accepted range.  If none is acceptable but a candidate in range failed :data:`PROBE`
    only for its modules, prints ``novenv <path>`` (for the message; it is not used).
    *owner*, *allowed* and *top* exist for tests; production uses the defaults.
    """
    allow = f"{shlex.quote(allowed)}/*"
    stay = (
        ""
        if top == "/"
        else f'    case "$d" in {shlex.quote(top)}|{shlex.quote(top)}/*) ;; *) ok=0; break ;; esac\n'
    )
    accepted = "|".join(accepted_versions())
    lines = (
        "novenv=\n"
        f"for n in {' '.join(shlex.quote(n) for n in names)}; do\n"
        '  p=$(command -v "$n" 2>/dev/null) || continue\n'
        '  case "$p" in /*) ;; *) continue ;; esac\n'
        '  r=$(readlink -f -- "$p" 2>/dev/null) || continue\n'
        f'  case "$r" in {allow}) ;; *) continue ;; esac\n'
        '  [ -f "$r" ] && [ -x "$r" ] || continue\n'
        "  d=$r; ok=1\n"
        "  while :; do\n"
        f"{stay}"
        '    if [ -L "$d" ] || [ -n "$(find "$d" -maxdepth 0 \\( ! -user '
        f"{shlex.quote(owner)}"
        ' -o -perm -020 -o -perm -002 \\) -print 2>&1)" ]; then ok=0; break; fi\n'
        f'    [ "$d" = {shlex.quote(top)} ] && break\n'
        '    d=${d%/*}; [ -n "$d" ] || d=/\n'
        "  done\n"
        '  [ "$ok" = 1 ] || continue\n'
        f'  v=$(cd / && "$r" -I -c {shlex.quote(PROBE)} 2>/dev/null) || {{\n'
        f'    v=$(cd / && "$r" -I -c {shlex.quote(VERSION_PROBE)} 2>/dev/null) &&\n'
        f'      case "$v" in {accepted}) novenv=${{novenv:-$r}} ;; esac\n'
        "    continue\n"
        "  }\n"
        f'  case "$v" in {accepted}) printf "%s\\n" "$r"; exit 0 ;; esac\n'
        "done\n"
        '[ -z "$novenv" ] || printf "novenv %s\\n" "$novenv"\n'
        "exit 0\n"
    )
    return lines


def installed_check_script(sha256: str) -> str:
    """Exit 0 and print :data:`PROBE` output if the pinned tree is intact and root-owned.

    Checks ``/``, ``/opt``, ``/opt/dango`` and :data:`PYTHON_HOME` (root-owned real
    directories), every entry of the tree (root-owned, not group/other writable), that no
    link in it resolves outside it, and the marker; only then runs the interpreter.
    """
    return (
        f"for d in /opt /opt/dango {PYTHON_HOME} {PYTHON_DIR}; do "
        '[ -d "$d" ] && [ ! -L "$d" ] || exit 1; done\n'
        f"bad=$(find / /opt /opt/dango {PYTHON_HOME} {PYTHON_DIR} -maxdepth 0 {_NOT_ROOTS} "
        "-print 2>&1\n"
        f"  find {PYTHON_DIR} {_TREE_UNTRUSTED} -print 2>&1 | head -n 5\n"
        f"  find {PYTHON_DIR} -type l -print | while IFS= read -r l; do\n"
        '    t=$(readlink -f -- "$l") || t=\n'
        f'    case "$t" in {PYTHON_DIR}/*) ;; *) printf "%s\\n" "$l" ;; esac\n'
        "  done)\n"
        '[ -z "$bad" ] || { printf "%s\\n" "$bad" >&2; exit 1; }\n'
        f'[ -f {PYTHON_DIR}/{MARKER} ] && [ "$(cat {PYTHON_DIR}/{MARKER})" = {sha256} ] || exit 1\n'
        f"cd / && {PYTHON_BIN} -I -c {shlex.quote(PROBE)}\n"
    )


def link_check_arm() -> str:
    """A ``case`` arm for the venv link check: a link into an interpreter Dango installed.

    The target's build directory must be a root-owned tree that nobody else can write, under
    root-owned ``/opt``, ``/opt/dango`` and :data:`PYTHON_HOME`; otherwise the link is printed.
    Runs inside the loop of ``runtime_ownership.links_outside_script`` (``$l`` the link,
    ``$t`` its resolved target).
    """
    return (
        f"({PYTHON_HOME}/*) g=${{t#{PYTHON_HOME}/}}; g={PYTHON_HOME}/${{g%%/*}}; "
        "b=$( { find / /opt /opt/dango "
        f'{PYTHON_HOME} "$g" -maxdepth 0 {_NOT_ROOTS} -print; '
        f'find "$g" {_TREE_UNTRUSTED} -print -quit; }} 2>&1 ); '
        'test -z "$b" || echo "$l -> $t"; continue;;'
    )


def download_script(build: PythonBuild) -> str:
    """Download *build* to ``"$work"/python.tar.gz`` and check its size (``sh``, as root).

    ``curl --max-filesize`` refuses a body with a larger ``Content-Length``; a body without
    one is capped at one byte over the pinned size by ``head -c`` (curl before 8.4 ignores
    ``--max-filesize`` there).  On failure ``"$work"`` is removed and the last line on
    stderr keeps curl's own error and exit code; curl exit 63 or a capped body exits with
    ``EXIT_SIZE``.
    """
    archive = '"$work"/python.tar.gz'
    fail = 'rm -rf -- "$work"; echo'
    return (
        "{ curl --proto '=https' --proto-redir '=https' --tlsv1.2 --fail --silent --show-error"
        f" --location --max-redirs 3 --max-filesize {build.size + SIZE_MARGIN}"
        f" --connect-timeout 30 --max-time {DOWNLOAD_MAX_TIME} {shlex.quote(build.url)}"
        ' 2>"$work"/curl.err; echo "$?" >"$work"/curl.rc; }'
        f" | head -c {build.size + 1} > {archive}\n"
        'rc=$(cat "$work"/curl.rc 2>/dev/null) || rc=\n'
        f"size=$(wc -c < {archive} | tr -d ' ')\n"
        'if [ "$rc" != 0 ]; then\n'
        '  err=$(tr -s "\\n" " " < "$work"/curl.err 2>/dev/null) || err=\n'
        f'  if [ "$rc" = 63 ] || [ "$size" -gt {build.size} ] 2>/dev/null; then\n'
        f'    {fail} "larger than {build.size} bytes (curl exit $rc: $err)" >&2; exit {EXIT_SIZE}\n'
        "  fi\n"
        f'  {fail} "curl exit ${{rc:-unknown}}: $err" >&2; exit {EXIT_DOWNLOAD}\n'
        "fi\n"
        f'[ "$size" = {build.size} ] || {{ {fail} "size $size, expected {build.size}" >&2; '
        f"exit {EXIT_SIZE}; }}\n"
    )


def install_script(build: PythonBuild) -> str:
    """Download, verify and install *build* at :data:`PYTHON_DIR` (``sh``, as root).

    Order: refuse a ``/opt/dango`` that is a link or not root's; take the install lock (one
    install at a time); remove staging directories an interrupted run left; download into a
    fresh ``mktemp -d`` under the root-owned :data:`PYTHON_HOME`; check size and SHA-256
    before anything is extracted (never piped into ``tar``); check the archive listing (names under
    ``python/``, no ``..``, only files, directories and symlinks); extract; re-check (no
    hard links, devices or links leading outside); make it root's and not group/other
    writable; write the marker; ``mv -T`` into place.  GNU/Linux tools (``mv -T``,
    ``sha256sum``, ``flock``, ``mktemp`` suffix); exits with the ``EXIT_*`` codes.
    """
    work = '"$work"'
    archive = f"{work}/python.tar.gz"
    tree = f"{work}/x/python"
    fail = 'rm -rf -- "$work"; echo'
    return (
        "set -u\n"
        "umask 022\n"
        "for d in / /opt /opt/dango "
        f"{PYTHON_HOME}; do\n"
        '  if [ -L "$d" ]; then echo "$d is a symbolic link" >&2; '
        f"exit {EXIT_LOCATION}; fi\n"
        '  if [ -e "$d" ] && { [ ! -d "$d" ] || [ -n "$(find "$d" -maxdepth 0 '
        f'{_NOT_ROOTS} -print 2>&1)" ]; }}; then\n'
        f'    echo "$d is not a root-owned directory closed to other users" >&2; exit {EXIT_LOCATION}\n'
        "  fi\n"
        "done\n"
        "for t in curl sha256sum tar find readlink mktemp flock; do\n"
        f'  command -v "$t" >/dev/null 2>&1 || {{ echo "$t is not installed" >&2; exit {EXIT_TOOLS}; }}\n'
        "done\n"
        f"install -d -o root -g root -m 0755 /opt/dango {PYTHON_HOME} || exit {EXIT_LOCATION}\n"
        f"exec 9>{PYTHON_HOME}/.install.lock || exit {EXIT_LOCATION}\n"
        f'flock -w {LOCK_WAIT} 9 || {{ echo "another Python install is running" >&2; exit {EXIT_LOCATION}; }}\n'
        f"for s in {PYTHON_HOME}/*.tmp; do\n"
        '  if [ -e "$s" ] || [ -L "$s" ]; then rm -rf -- "$s"; fi\n'
        "done\n"
        f"work=$(mktemp -d {PYTHON_HOME}/stage.XXXXXXXXXX.tmp) || exit {EXIT_LOCATION}\n"
        f"{download_script(build)}"
        f"printf '%s  %s\\n' {build.sha256} {archive} | sha256sum -c --status"
        f' || {{ {fail} "SHA-256 mismatch" >&2; exit {EXIT_CHECKSUM}; }}\n'
        f"tar -tzf {archive} > {work}/names && tar -tvzf {archive} > {work}/types"
        f' || {{ {fail} "unreadable archive" >&2; exit {EXIT_ARCHIVE}; }}\n'
        f"if grep -v '^python/' {work}/names >/dev/null"
        f" || grep -E '(^|/)[.][.](/|$)' {work}/names >/dev/null"
        f" || cut -c1 {work}/types | grep -v '^[-dl]$' >/dev/null; then\n"
        f'  {fail} "unexpected archive entries" >&2; exit {EXIT_ARCHIVE}\n'
        "fi\n"
        f"mkdir {work}/x && tar -xzf {archive} -C {work}/x --no-same-owner --no-same-permissions"
        f' || {{ {fail} "extraction failed" >&2; exit {EXIT_ARCHIVE}; }}\n'
        f"bad=$( {{ find {work}/x ! -type f ! -type d ! -type l -print; "
        f"find {work}/x -type f -links +1 -print; "
        f"find {tree} -type l -print | while IFS= read -r l; do "
        't=$(readlink -f -- "$l") || t=; '
        f'case "$t" in {tree}/*) ;; *) printf "%s\\n" "$l" ;; esac; done; }} 2>&1 )\n'
        f'if [ -z "$bad" ] && [ -d {tree} ] && [ ! -L {tree} ]; then :; else '
        f'{fail} "unsafe archive content" >&2; exit {EXIT_ARCHIVE}; fi\n'
        f"chown -R root:root {work}/x && chmod -R go-w {work}/x"
        f" && printf '%s\\n' {build.sha256} > {tree}/{MARKER} && chmod 0644 {tree}/{MARKER}"
        f' || {{ {fail} "could not secure the tree" >&2; exit {EXIT_LOCATION}; }}\n'
        f"if [ -e {PYTHON_DIR} ] || [ -L {PYTHON_DIR} ]; then mv -T {PYTHON_DIR} {work}/old"
        f' || {{ {fail} "could not replace {PYTHON_DIR}" >&2; exit {EXIT_LOCATION}; }}; fi\n'
        f"mv -T {tree} {PYTHON_DIR}"
        f' || {{ {fail} "could not move the tree into place" >&2; exit {EXIT_LOCATION}; }}\n'
        'rm -rf -- "$work"\n'
    )
