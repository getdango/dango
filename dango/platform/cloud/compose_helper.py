"""dango/platform/cloud/compose_helper.py

Root-owned compose helper for cloud servers (not yet enabled on any server).

The helper is a small POSIX ``sh`` program installed as ``root:root 0755``.  The
service account may call it through ``sudo -n``; it accepts a fixed verb set,
reads the compose file, project directory and project name only from root-owned
files, clears the environment, and runs every verb under ``timeout`` so a caller
that gives up never leaves a queued call behind.  This module holds the helper
text, the sudoers rule, the install scripts and the command wrapper that root-side
callers will use once the helper is enabled.
"""

from __future__ import annotations

import shlex

SRV_DIR = "/srv/dango"
COMPOSE_DIR = f"{SRV_DIR}/compose"
BUILD_DIR = f"{COMPOSE_DIR}/build"
METABASE_DIR = f"{SRV_DIR}/metabase"
PLUGINS_DIR = f"{METABASE_DIR}/plugins"
LOCK_DIR = "/run/dango-compose"
HELPER_PATH = "/usr/local/sbin/dango-compose"
SUDOERS_PATH = "/etc/sudoers.d/dango-compose"
#: Present (root-owned) only once the helper is installed, verified and in use.
MARKER = f"{COMPOSE_DIR}/active"

SUDOERS_TEXT = (
    "# Installed by Dango. The dango account may run only the root-owned compose helper;"
    " the helper checks every argument.\n"
    f"dango ALL=(root) NOPASSWD: {HELPER_PATH} *\n"
)

#: Fixed ids the server render gives the containers (uid = gid); reserved, never host ids.
METABASE_ID = 61000
DOCS_ID = 61001
CONTAINER_IDS = (METABASE_ID, DOCS_ID)

#: Lock wait in seconds: short through sudo, long for root's own steps.
SUDO_LOCK_WAIT = 30
ROOT_LOCK_WAIT = 900
#: Seconds a caller's own timeout must exceed (lock wait + budget) by, so that the
#: helper (``timeout -k 10``) is always gone before the caller gives up.
CALLER_MARGIN = 15

#: Operation budget per verb (seconds), excluding the lock wait.
VERB_BUDGET: dict[str, int] = {
    "self-test": 120,
    "up": 420,
    "down": 120,
    "stop": 45,
    "start": 90,
    "restart": 120,
    "ps": 30,
    "image-exists": 30,
    "h2-size": 30,
    "logs": 30,
    "reset-password": 120,
    "render": 420,
    "pin-project": 60,
    "up-build": 900,
    "pull": 600,
    "build": 900,
    "down-volumes": 120,
    "down-rmi": 180,
    "destroy": 300,
    "volume-path": 30,
}
#: Verbs that take the lock (the others only read).
LOCKED_VERBS = frozenset(
    {
        "up",
        "down",
        "stop",
        "start",
        "restart",
        "reset-password",
        "pin-project",
        "up-build",
        "pull",
        "build",
        "down-volumes",
        "down-rmi",
        "destroy",
    }
)


def min_caller_timeout(verb: str) -> int:
    """Smallest subprocess timeout a dango-side caller may use for *verb* via sudo."""
    wait = SUDO_LOCK_WAIT if verb in LOCKED_VERBS else 0
    return wait + VERB_BUDGET[verb] + CALLER_MARGIN


_SCRIPT = r"""#!/bin/sh
# Installed by Dango. The only way the dango account operates Docker; every argument is checked here.
set -eu
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; export PATH
umask 022
S=/srv/dango; D=$S/compose; L=/run/dango-compose
SUDO_WAIT=@SUDO_WAIT@; ROOT_WAIT=@ROOT_WAIT@
GENV="env -i PATH=$PATH HOME=/root LANG=C.UTF-8"
die() { printf 'dango-compose: %s\n' "$*" >&2; exit 64; }
[ "$(id -u)" = 0 ] || die "run as root or via sudo -n"
via_sudo() { [ -n "${SUDO_UID:-}" ] && [ "$SUDO_UID" != 0 ]; }
root_only() { ! via_sudo || die "$verb: root only"; }
argc() { [ "$1" -eq "$2" ] || die "usage: $verb"; }
budget() { case "$1" in
@BUDGET_CASES@
  *) echo 60;; esac; }
svc() { case "${1:-}" in metabase|dbt-docs) ;; *) die "unknown service";; esac; }
num() { case "$1" in ''|0?*|*[!0-9]*) return 1;; esac; [ "${#1}" -le "$2" ]; }
port() { num "$1" 5 && [ "$1" -ge 1024 ] && [ "$1" -le 65535 ] || die "bad port"; }
ts_ok() { case "$1" in [0-9][0-9][0-9][0-9]-*) ;; *) return 1;; esac
  case "$1" in *[!0-9T:.+Z-]*) return 1;; esac; [ "${#1}" -le 40 ]; }
email_ok() { case "$1" in -*|''|*[!A-Za-z0-9._%+@-]*|*@*@*) return 1;; ?*@?*.?*) ;; *) return 1;; esac
  [ "${#1}" -le 254 ]; }
id_ok() { case "$1" in [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;; *) return 1;; esac
  case "$1" in *[!0-9a-f-]*) return 1;; esac; [ "${#1}" -le 36 ]; }
own() { [ -n "$(find "$1" -maxdepth 0 ! -type l -user root ! -perm /022 2>/dev/null)" ] || die "$1 not root-owned"; }
name() { own "$D/project"; n=$(cat "$D/project")
  case "$n" in dango-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;; *) die "bad project";; esac; }
base() { own "$S"; own "$D"; own "$D/build"; own "$S/metabase"
  [ -n "$(find "$S/metabase" -maxdepth 0 -type d -user root -perm 0700 2>/dev/null)" ] || die "$S/metabase not private"; }
ids() { command -v getent >/dev/null || { echo "dango-compose: getent missing" >&2; exit 67; }
  for i in @CONTAINER_IDS@; do
    [ -z "$(getent passwd "$i" 2>/dev/null)" ] && [ -z "$(getent group "$i" 2>/dev/null)" ] ||
      { echo "dango-compose: container id $i is already used on this host" >&2; exit 67; }
  done
  g=$(getent group docker 2>/dev/null | cut -d: -f3)
  case " @CONTAINER_IDS@ " in *" $g "*) echo "dango-compose: docker group id is a container id" >&2; exit 67;; esac; }
lockdir() { mkdir -m 0700 "$L" 2>/dev/null || [ -d "$L" ] || die "$L unsafe"
  [ -n "$(find "$L" -maxdepth 0 -type d -user root -perm 0700 2>/dev/null)" ] || die "$L unsafe"
  [ -e "$L/lock" ] || { umask 077; : >"$L/lock"; umask 022; }
  [ -n "$(find "$L/lock" -maxdepth 0 -type f -user root -perm 0600 2>/dev/null)" ] || die "$L/lock unsafe"; }
run() { t=$1; shift; rc=0; k=0; cp=
  trap 'k=1; [ -z "$cp" ] || kill -TERM "$cp" 2>/dev/null' HUP INT TERM
  timeout -k 10 "$t" "$@" </dev/null 9>&- & cp=$!
  # A signal that arrived before the pid was known; repeated because the forked child can still
  # be running the shell's own handler until it execs, which would swallow a single TERM.
  [ "$k" = 0 ] || for _ in 1 2 3; do kill -TERM "$cp" 2>/dev/null; sleep 0.1; done
  while :; do k=0; wait $cp && rc=0 || rc=$?; [ "$k" = 1 ] || break; done
  trap - HUP INT TERM
  case $rc in
    124|137) printf 'dango-compose: %s exceeded its time budget (%ss)\n' "$verb" "$t" >&2;;
  esac; exit "$rc"; }
# The lock lives on this sh's fd 9 (the workload does not inherit it): a SIGKILL of the sh releases
# it while timeout/docker may run on for up to budget + 10 s. Accepted.
locked() { lockdir; w=$ROOT_WAIT; ! via_sudo || w=$SUDO_WAIT
  exec 9>>"$L/lock"
  flock -w "$w" -E 75 9 || { rc=$?; [ "$rc" != 75 ] ||
    printf 'dango-compose: busy (another Dango Docker operation is running)\n' >&2; exit "$rc"; }
  run "$b" "$@"; }
dc() { m=$1; shift; base; name; own "$D/docker-compose.yml"
  if [ "$m" = lock ]; then
    locked $GENV docker compose --project-directory "$D" -p "$n" -f "$D/docker-compose.yml" "$@"
  else
    run "$b" $GENV docker compose --project-directory "$D" -p "$n" -f "$D/docker-compose.yml" "$@"
  fi; }
verb=${1:-}; [ "$#" -eq 0 ] || shift
b=$(budget "$verb")
case "$verb" in
  self-test)     argc $# 0; cd /; base; own "$S/venv"
                 command -v flock >/dev/null || die "flock missing"
                 command -v timeout >/dev/null || die "timeout missing"
                 ids
                 timeout -k 5 60 $GENV docker compose version --short >/dev/null 2>&1 || die "compose v2 missing"
                 timeout -k 5 60 $GENV "$S/venv/bin/python" -I -c 'import dango.platform.cloud.server_compose' \
                   >/dev/null 2>&1 || die "server code"
                 echo ok ;;
  up)            argc $# 0; dc lock up -d ;;
  down)          argc $# 0; dc lock down ;;
  stop|start)    [ $# -le 1 ] || die "usage: $verb"; [ $# -eq 0 ] || svc "$1"; dc lock "$verb" "$@" ;;
  restart)       argc $# 1; svc "$1"; dc lock restart "$1" ;;
  ps)            argc $# 0; dc nolock ps --format json ;;
  image-exists)  argc $# 0; dc nolock images -q metabase ;;
  h2-size)       argc $# 0; dc nolock exec -T metabase stat -c %s /metabase-data/metabase.db.mv.db ;;
  logs)          [ $# -ge 1 ] || die "usage: logs"; svc "$1"; s=$1; shift; since=; lines=
                 while [ $# -gt 0 ]; do case "$1" in
                   --since) [ $# -ge 2 ] && [ -z "$since" ] || die "usage: logs"; ts_ok "$2" || die "bad timestamp"
                            since=$2; shift 2;;
                   --tail)  [ $# -ge 2 ] && [ -z "$lines" ] || die "usage: logs"
                            num "$2" 5 && [ "$2" -ge 1 ] || die "bad count"; lines=$2; shift 2;;
                   *) die "usage: logs";; esac; done
                 set -- logs --no-color
                 [ -z "$since" ] || set -- "$@" --since "$since"
                 [ -z "$lines" ] || set -- "$@" --tail "$lines"
                 dc nolock "$@" "$s" ;;
  reset-password) argc $# 1; email_ok "$1" || die "bad email"
                 dc lock run --rm --no-deps -T metabase reset-password "$1" ;;
  render)        argc $# 4; port "$1"; port "$2"; [ "$1" != "$2" ] || die "bad port"
                 case "${3#v}" in ''|*[!0-9.]*) die "bad version";; esac
                 case "$3" in v[0-9]*) ;; *) die "bad version";; esac
                 [ "${#3}" -le 20 ] || die "bad version"
                 ids; base; name; own "$D/project-id"; own "$S/venv"
                 cd /; run "$b" env -i PATH="$PATH" LANG=C.UTF-8 "$S/venv/bin/python" -I \
                   -m dango.platform.cloud.server_compose "$@" ;;
  pin-project)   root_only
                 if [ $# -eq 1 ]; then force=0
                 elif [ $# -eq 2 ] && [ "$2" = --replace ]; then force=1
                 else die "usage: pin-project"; fi
                 id_ok "$1" || die "bad id"; base; [ ! -e "$D/project" ] || own "$D/project"
                 p="dango-$(printf %s "$1" | cut -c1-8)"
                 locked sh -c 'D=$1
                   if [ -e "$D/project" ] && [ "$(cat "$D/project")" != "$3" ] && [ "$4" != 1 ]; then
                     echo "dango-compose: project pinned" >&2; exit 64; fi
                   printf "%s\n" "$2" >"$D/.pid.tmp" && mv "$D/.pid.tmp" "$D/project-id" &&
                   printf "%s\n" "$3" >"$D/.p.tmp" && mv "$D/.p.tmp" "$D/project"' _ "$D" "$1" "$p" "$force" ;;
  up-build)      root_only; argc $# 0; dc lock up -d --build metabase ;;
  pull|build)    root_only; argc $# 0; dc lock "$verb" ;;
  down-volumes)  root_only; argc $# 1; [ "$1" = metabase ] || die "usage: down-volumes"
                 dc lock down metabase --volumes ;;
  down-rmi)      root_only; argc $# 0; dc lock down --rmi local ;;
  destroy)       root_only; argc $# 0; dc lock down --volumes --rmi local ;;
  volume-path)   root_only; argc $# 0; base; name
                 run "$b" $GENV docker volume inspect "${n}_metabase-data" --format '{{.Mountpoint}}' ;;
  *)             die "unknown verb" ;;
esac
"""


def helper_script() -> str:
    """The helper's text (a constant apart from the budget table and lock waits)."""
    cases = "\n".join(f"  {verb}) echo {secs};;" for verb, secs in VERB_BUDGET.items())
    return (
        _SCRIPT.replace("@BUDGET_CASES@", cases)
        .replace("@SUDO_WAIT@", str(SUDO_LOCK_WAIT))
        .replace("@ROOT_WAIT@", str(ROOT_LOCK_WAIT))
        .replace("@CONTAINER_IDS@", " ".join(str(i) for i in CONTAINER_IDS))
    )


def _heredoc(path_tmp: str, text: str, tag: str) -> str:
    if tag in text:
        raise ValueError("heredoc tag appears in the text")
    return f"cat >{path_tmp} <<'{tag}'\n{text}{tag}\n"


def install_dirs_script() -> str:
    """Create the root-owned layout; refuses symlinks at every managed path."""
    paths = (COMPOSE_DIR, BUILD_DIR, METABASE_DIR, PLUGINS_DIR, HELPER_PATH, SUDOERS_PATH)
    return (
        f"for p in {' '.join(paths)}; do "
        'if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi; done\n'
        f"install -d -o root -g root -m 0755 {SRV_DIR} {COMPOSE_DIR} {BUILD_DIR}\n"
        f"install -d -o root -g root -m 0700 {METABASE_DIR} {PLUGINS_DIR}\n"
    )


def install_helper_script() -> str:
    """Install the helper: write a temp file, set owner and mode, then move into place."""
    tmp = f"{HELPER_PATH}.tmp"
    return (
        "install -d -o root -g root -m 0755 /usr/local/sbin\n"
        + _heredoc(tmp, helper_script(), "DANGO_COMPOSE_HELPER_EOF")
        + f"chown root:root {tmp} || exit 4\nchmod 0755 {tmp} || exit 4\nmv -f {tmp} {HELPER_PATH} || exit 4\n"
    )


def install_sudoers_script() -> str:
    """Install the sudoers rule: temp file, 0440, ``visudo -cf``, move, ``visudo -c``, ``sudo -l``."""
    tmp = f"{SUDOERS_PATH}.tmp"
    return (
        "install -d -o root -g root -m 0750 /etc/sudoers.d\n"
        + _heredoc(tmp, SUDOERS_TEXT, "DANGO_SUDOERS_EOF")
        + f"chown root:root {tmp} || exit 4\nchmod 0440 {tmp} || exit 4\n"
        f"visudo -cf {tmp} >/dev/null || {{ rm -f {tmp}; echo 'sudoers check failed' >&2; exit 4; }}\n"
        f"mv -f {tmp} {SUDOERS_PATH}\n"
        "visudo -c >/dev/null || { rm -f "
        + SUDOERS_PATH
        + "; echo 'sudoers check failed' >&2; exit 4; }\n"
        f"sudo -l -U dango 2>/dev/null | grep -qF {shlex.quote(HELPER_PATH)}"
        " || { echo 'sudoers rule not active' >&2; exit 4; }\n"
    )


def install_script() -> str:
    """Whole root-side install (layout, helper, sudoers), as one ``set -e`` script."""
    return "set -e\n" + install_dirs_script() + install_helper_script() + install_sudoers_script()


def self_test_command() -> str:
    """Command run as the service user to prove sudo rule, helper and server code work."""
    return f"runuser -u dango -- sudo -n {HELPER_PATH} self-test"


def server_compose_cmd(verb: str, *args: str, legacy: str) -> str:
    """Root-side command: the helper when the marker is present, else *legacy* unchanged."""
    if verb not in VERB_BUDGET:
        raise ValueError(f"unknown helper verb: {verb}")
    helper = " ".join(shlex.quote(p) for p in (HELPER_PATH, verb, *args))
    return f"if [ -f {MARKER} ] && [ ! -L {MARKER} ]; then {helper}; else {legacy}; fi"
