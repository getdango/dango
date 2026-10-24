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
    snapshot directory's default ACL.  Exits 3 for a symlink at a managed path and non-zero
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
                'mkdir -p "$P/notebooks"',
                'for d in "$P/.dango" "$P/.dlt"; do [ ! -d "$d" ] || chmod 0700 "$d"; done',
                '[ ! -e "$P/.env" ] || chmod 0600 "$P/.env"',
                'for p in "$P/.dango" "$P/.dlt" "$P/.env"; do',
                f'  [ ! -e "$p" ] || setfacl -m {_DENY} "$p"',
                "done",
                f'[ ! -d "$H" ] || setfacl -m {_DENY} "$H"',
                'if [ -L "$P/data" ]; then d=$(readlink -f "$P/data"); else d="$P/data"; fi',
                f'[ ! -d "$d" ] || setfacl -m u:{NB_USER}:--- "$d"',
                f'setfacl -m {SNAPSHOT_DEFAULT_ACL} "$SNAP"',
            ]
        )
        + "\n"
    )


def notebooks_acl_script(project: str | None = None, srv_dir: str = SRV_DIR) -> str:
    """Notebook-runner ACL on ``notebooks/`` and its ``.py`` files, run as the service account.

    Also the re-apply script after a restore recreates the directory.  Existing files are
    found with ``find -type f`` and changed with ``setfacl -P`` so a link planted in the
    directory is not followed.  A ``setfacl`` failure is fatal for the notebook runner.
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
                f"find \"$N\" -type f -name '*.py' -exec setfacl -P -m {NOTEBOOK_FILE_ACL} {{}} +",
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
                'if [ -L "$P/data" ]; then d=$(readlink -f "$P/data"); else d="$P/data"; fi',
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
                "fi",
                f'runuser -u {SERVICE_USER} -- sh -c {q(probe)} _ "$P/notebooks"'
                ' || fail "setfacl does not work on the project filesystem"',
                'exit "$bad"',
            ]
        )
        + "\n"
    )
