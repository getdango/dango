"""dango/platform/cloud/runner_access_scripts.py

Shell-text builders for what the runner users may read and write inside the project, and
for the post-install verification.  Pure text, nothing here is run or called yet.

The permission scripts act on paths the service account owns, so they are meant to run
as that account (``runuser -u dango --``, see ``runner_scripts.as_service_user``): an
owner may set modes and ACLs, and root never follows a link that account controls.  Each
managed path is refused when it is a symlink; ``data`` may legitimately be one (block
storage), and then the ACL goes on its target.  The verification script runs as root
and only reads the project, apart from commands wrapped in ``runuser``.
"""

from __future__ import annotations

import posixpath
from collections.abc import Sequence

from dango.platform.cloud.runner_scripts import (
    FORBIDDEN_GROUPS,
    NB_USER,
    RESERVED_IDS,
    RUN_USER,
    RUNNER_USERS,
    SERVICE_USER,
    SRV_DIR,
    UID_MAX,
    UID_MIN,
    q,
    runner_dir,
)
from dango.platform.run_as import PYTHON

PROJECT_SUBDIR = "project"
#: Absolute paths (besides the project) the runners are denied by ACL.
HOME_DIR = "/home/dango"
_DENY = f"u:{RUN_USER}:---,u:{NB_USER}:---"
#: Named-user entries for the notebook directory (``d:`` entries are inherited).
NOTEBOOKS_ACL = f"u:{NB_USER}:rwx,d:u:{NB_USER}:rwx,d:u:{SERVICE_USER}:rwx,d:m::rwx,m::rwx"
NOTEBOOK_FILE_ACL = f"u:{NB_USER}:rw,m::rw"
#: Default ACL for the snapshot directory: new files stay readable by group ``dango-nb`` only.
SNAPSHOT_DEFAULT_ACL = "d:u::rwx,d:g::r-x,d:o::---"


def project_dir(srv_dir: str = SRV_DIR) -> str:
    """The project directory under *srv_dir*."""
    return f"{srv_dir}/{PROJECT_SUBDIR}"


def project_permission_script(project: str | None = None, srv_dir: str = SRV_DIR) -> str:
    """Modes and deny ACLs for the project, to run as the service account.

    ``.dango`` and ``.dlt`` 0700, ``.env`` 0600, deny ACL for both runners on those and on
    the service account's home, ``data`` denied to the notebook runner only, and the
    snapshot directory's default ACL.  Missing protected paths (``.dango``, ``.dango/state``,
    ``.dlt``, ``.env``, ``data``, ``notebooks``) are created first, so a later writer lands
    inside an already denied path; a ``data`` symlink (block storage) gets the ACL on its
    target.  Exits 3 for a symlink at a managed path and non-zero
    when ``setfacl`` fails (no ACL support means no isolation).
    """
    proj = project if project is not None else project_dir(srv_dir)
    snap = f"{runner_dir(srv_dir)}/snapshots"
    return (
        "\n".join(
            [
                "set -eu",
                f"P={q(proj)}",
                f"SNAP={q(snap)}",
                f"H={q(HOME_DIR)}",
                'for p in "$P/.dango" "$P/.dlt" "$P/.env" "$P/notebooks" "$SNAP" "$H"; do',
                '  if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi',
                "done",
                # create what is missing first, so later writers land inside denied paths
                'if [ -L "$P/.dango/state" ]; then echo "refusing: state is a symlink" >&2; exit 3; fi',
                'for d in "$P/.dango" "$P/.dango/state" "$P/.dlt"; do',
                '  [ -d "$d" ] || mkdir "$d"; chmod 0700 "$d"',
                "done",
                '[ -e "$P/.env" ] || (umask 077; : >"$P/.env")',
                'chmod 0600 "$P/.env"',
                '[ -e "$P/data" ] || [ -L "$P/data" ] || mkdir -m 0755 "$P/data"',
                'mkdir -p "$P/notebooks"',
                'for p in "$P/.dango" "$P/.dlt" "$P/.env"; do',
                f'  setfacl -m {_DENY} "$p"',
                "done",
                f'[ ! -d "$H" ] || setfacl -m {_DENY} "$H"',
                'if [ -L "$P/data" ]; then d=$(readlink -f "$P/data") || d=""; else d="$P/data"; fi',
                f'[ ! -d "$d" ] || setfacl -m u:{NB_USER}:--- "$d"',
                f'setfacl -m {SNAPSHOT_DEFAULT_ACL} "$SNAP"',
            ]
        )
        + "\n"
    )


def notebooks_acl_script(project: str | None = None, srv_dir: str = SRV_DIR) -> str:
    """Notebook-runner ACL on ``notebooks/`` and its ``.py`` files, run as the service account.

    Also the re-apply script after a restore recreates the directory.  Existing files are
    found with ``find -P -type f`` and changed with ``-execdir`` (``setfacl -P ./name`` in the
    file's own directory), so a directory swapped for a link after it was listed is never
    entered by name.  The remaining last-component window is closed by running only while no
    runner process exists (:func:`quiesce_script`).  A ``setfacl`` failure is fatal.
    """
    proj = project if project is not None else project_dir(srv_dir)
    return (
        "\n".join(
            [
                "set -eu",
                f"N={q(proj)}/notebooks",
                'if [ -L "$N" ]; then echo "refusing: $N is a symlink" >&2; exit 3; fi',
                'mkdir -p "$N"',
                f'setfacl -m {NOTEBOOKS_ACL} "$N"',
                f"find -P \"$N\" -type f -name '*.py' -execdir setfacl -P -m {NOTEBOOK_FILE_ACL} {{}} +",
            ]
        )
        + "\n"
    )


def ownership_precheck_script(
    srv_dir: str = SRV_DIR,
    *,
    owner: str = "root",
    paths: Sequence[str] | None = None,
) -> str:
    """Root-side check, first of all steps: ``/``, ``/srv`` and *srv_dir* are trusted.

    Each path must exist, not be a symlink, belong to *owner* and be writable by neither group
    nor others.  Otherwise the service account could swap what root is about to create or
    ``chown`` under *srv_dir*.  Exits 7 and prints the path.  *paths* is for tests.
    """
    wanted = list(paths) if paths is not None else ["/", posixpath.dirname(srv_dir), srv_dir]
    unique = list(dict.fromkeys(wanted))
    listing = " ".join(q(p) for p in unique)
    return (
        "\n".join(
            [
                "set -eu",
                f"for p in {listing}; do",
                '  [ -e "$p" ] || { echo "ownership check: $p is missing" >&2; exit 7; }',
                f'  hit=$(find "$p" -maxdepth 0 \\( -type l -o ! -user {q(owner)}'
                " -o -perm -0020 -o -perm -0002 \\) -print)"
                ' || { echo "ownership check of $p failed" >&2; exit 7; }',
                '  [ -z "$hit" ] || { echo "refusing: $p is a link, not owned by'
                f' {owner}, or writable by others" >&2; exit 7; }}',
                "done",
            ]
        )
        + "\n"
    )


def quiesce_script(*, stop_web: bool = True) -> str:
    """Root-side: make sure no runner process exists, optionally after stopping ``dango-web``.

    Permissions are re-applied only while no runner can race them.  Kills every process of
    both runner users (``pkill -KILL -u``), repeats until ``pgrep -u`` finds none, and exits 8
    when one survives or ``dango-web`` is still active after its stop.  Users that do not
    exist yet are skipped.
    """
    lines = ["set -u"]
    if stop_web:
        lines.append("systemctl stop dango-web >/dev/null 2>&1 || true")
        lines.append(
            "if systemctl is-active --quiet dango-web; then"
            ' echo "dango-web is still running" >&2; exit 8; fi'
        )
    lines += [
        f"for u in {' '.join(RUNNER_USERS)}; do",
        '  getent passwd "$u" >/dev/null 2>&1 || continue',
        "  n=0",
        '  while pgrep -u "$u" >/dev/null 2>&1; do',
        '    [ "$n" -lt 25 ] || { echo "processes of $u are still running" >&2; exit 8; }',
        '    pkill -KILL -u "$u" >/dev/null 2>&1 || true',
        "    n=$((n+1)); sleep 0.2",
        "  done",
        "done",
    ]
    return "\n".join(lines) + "\n"


def code_permission_checks(project: str | None = None, srv_dir: str = SRV_DIR) -> str:
    """Verify fragment: pushed code and the venv cannot be changed by others or by a runner.

    Fails (via the caller's ``fail``) when something under ``scripts``, ``custom_sources``,
    ``dbt`` or ``.dango`` is other-writable, when either runner can write those directories,
    or when the venv has group- or other-writable entries.  Symlinks are skipped (their own
    mode is meaningless).
    """
    proj = project if project is not None else project_dir(srv_dir)
    users = " ".join(RUNNER_USERS)
    return (
        "\n".join(
            [
                f"P={q(proj)}",
                f"V={q(srv_dir + '/venv')}",
                'for d in "$P/scripts" "$P/custom_sources" "$P/dbt" "$P/.dango"; do',
                '  [ -e "$d" ] || continue',
                '  hit=$(find "$d" ! -type l -perm -0002 -print 2>/dev/null | head -n 1)',
                '  [ -z "$hit" ] || fail "$hit is writable by everyone"',
                f"  for u in {users}; do",
                '    if runuser -u "$u" -- test -w "$d"; then fail "$u can write $d"; fi',
                "  done",
                "done",
                'if [ -d "$V" ]; then',
                '  hit=$(find "$V" ! -type l \\( -perm -0002 -o -perm -0020 \\) -print 2>/dev/null'
                " | head -n 1)",
                '  [ -z "$hit" ] || fail "$hit in the venv is writable by group or others"',
                f"  for u in {users}; do",
                '    if runuser -u "$u" -- test -w "$V"; then fail "$u can write the venv"; fi',
                "  done",
                "fi",
            ]
        )
        + "\n"
    )


def protected_path_checks(project: str | None = None, srv_dir: str = SRV_DIR) -> str:
    """Verify fragment: the protected paths have their modes and both runners are shut out."""
    proj = project if project is not None else project_dir(srv_dir)
    users = " ".join(RUNNER_USERS)
    return (
        "\n".join(
            [
                f"P={q(proj)}",
                'for p in "$P/.dango" "$P/.dlt"; do',
                '  [ "$(stat -c %a "$p")" = 700 ] || fail "$p mode is not 700"',
                "done",
                '[ "$(stat -c %a "$P/.env")" = 600 ] || fail "$P/.env mode is not 600"',
                'for p in "$P/.dango" "$P/.dlt" "$P/.env"; do',
                f"  for u in {users}; do",
                '    if runuser -u "$u" -- test -r "$p"; then fail "$u can read $p"; fi',
                '    if runuser -u "$u" -- test -x "$p"; then fail "$u can reach $p"; fi',
                "  done",
                "done",
            ]
        )
        + "\n"
    )


def verify_script(project: str | None = None, srv_dir: str = SRV_DIR) -> str:
    """Root-side checks that isolation works; prints each failure and exits 1 if any.

    1. ``isolated_run --check-only`` exits 0 as each runner, started the way the service does;
    2. neither runner has a sudo entry;
    3. runners are in none of the forbidden groups;
    4. runner ids are in range, not reserved, and 61000/61001 have no host entry;
    5. the container uids can still read ``data`` (other bits ``r-x``);
    6. ``setfacl`` works on the project filesystem.
    """
    proj = project if project is not None else project_dir(srv_dir)
    reserved = "|".join(str(i) for i in RESERVED_IDS)
    forbidden = " ".join(FORBIDDEN_GROUPS)
    users = " ".join(RUNNER_USERS)
    probe = (
        't=$(mktemp "$1/.acl-probe.XXXXXX") || exit 1; '
        f'setfacl -m u:{NB_USER}:r "$t"; rc=$?; rm -f "$t"; exit $rc'
    )
    return (
        "\n".join(
            [
                "set -u",
                f"P={q(proj)}",
                "bad=0",
                'fail() { echo "verify: $*" >&2; bad=1; }',
                f"for r in script:{RUN_USER} nb:{NB_USER}; do",
                "  role=${r%%:*}; user=${r#*:}",
                f'  out=$(cd / && runuser -u {SERVICE_USER} -- sudo -n -u "$user" {PYTHON} -I -m'
                ' dango.runtime.isolated_run --role "$role" --cwd "$P" --check-only 2>&1)'
                ' || fail "check-only failed as $user: $out"',
                "done",
                f"for u in {users}; do",
                '  if runuser -u "$u" -- sudo -n -l >/dev/null 2>&1; then fail "$u has sudo rights"; fi',
                '  out=$(sudo -n -l -U "$u" 2>&1 || true)',
                '  case "$out" in *"may run the following"*) fail "$u has a sudo rule";; esac',
                '  for g in $(id -nG "$u"); do',
                f'    case " {forbidden} " in *" $g "*) fail "$u is in group $g";; esac',
                "  done",
                '  for v in "$(id -u "$u")" "$(id -g "$u")"; do',
                f'    case "$v" in {reserved}) fail "$u has reserved id $v";; esac',
                f'    [ "$v" -ge {UID_MIN} ] && [ "$v" -lt {UID_MAX} ] || fail "$u id $v out of range"',
                "  done",
                '  [ "$(getent group "$(id -g "$u")" | cut -d: -f1)" = "$u" ] || fail "$u group id is shared"',
                "done",
                "for i in 61000 61001; do",
                '  [ -z "$(getent group "$i")" ] || fail "group id $i is in use on the host"',
                "done",
                'if [ -L "$P/data" ]; then d=$(readlink -f "$P/data") || d=""; else d="$P/data"; fi',
                'if [ -d "$d" ]; then',
                "  for m in 61000 999; do",
                "    if command -v setpriv >/dev/null 2>&1; then",
                "      setpriv --reuid=$m --regid=$m --clear-groups sh -c"
                ' \'test -r "$1" && test -x "$1"\' _ "$d"'
                ' || fail "uid $m cannot read data"',
                "    else",
                '      o=$(stat -c %a "$d" | tail -c 2)',
                '      [ "$((o & 5))" -eq 5 ] || fail "data lacks other r-x"',
                "    fi",
                "  done",
                f'  runuser -u {NB_USER} -- test -x "$d" && fail "dango-nb can reach data"',
                "fi",
                protected_path_checks(proj).rstrip("\n"),
                code_permission_checks(proj, srv_dir).rstrip("\n"),
                f'runuser -u {SERVICE_USER} -- sh -c {q(probe)} _ "$P/notebooks"'
                ' || fail "setfacl does not work on the project filesystem"',
                'exit "$bad"',
            ]
        )
        + "\n"
    )
