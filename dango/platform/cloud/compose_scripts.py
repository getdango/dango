"""dango/platform/cloud/compose_scripts.py

Shell scripts and constants for switching a server to the compose helper (see ``compose_migrate``).

Every script starts with a ``# step:XX`` tag so logs and tests can tell the steps apart.  Scripts
only check and change root-owned state; they take no input from the service account.
"""

from __future__ import annotations

import json
import re
import shlex

from dango.platform.cloud import compose_helper as ch
from dango.platform.cloud.runtime_ownership import OwnershipStep

RECORD_PATH = f"{ch.SRV_DIR}/server-version.json"
PROJECT_DIR = f"{ch.SRV_DIR}/project"
VENV_PYTHON = f"{ch.SRV_DIR}/venv/bin/python"
#: Uid/gid the Metabase user had in the image built before the helper (a system account).
OLD_METABASE_ID = 999

ACCESS_HELPER = "helper"
ACCESS_GROUP = "group"
ACCESS_MIGRATING = "migrating"
_ACCESS = (ACCESS_HELPER, ACCESS_GROUP, ACCESS_MIGRATING)
_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+!_-]{0,31}")
ID_RE = re.compile(r"[0-9a-f]{8}[0-9a-f-]{0,28}")


def release_of(version: str | None) -> tuple[int, ...] | None:
    """The numeric release tuple of *version*, or None when it is not a version."""
    from packaging.version import InvalidVersion, Version

    if not version:
        return None
    try:
        return Version(version).release
    except InvalidVersion:
        return None


def _record(version: str, access: str) -> str:
    if not _VERSION_RE.fullmatch(version) or release_of(version) is None:
        raise ValueError(f"not a version: {version!r}")
    if access not in _ACCESS:
        raise ValueError(f"unknown docker access: {access!r}")
    return json.dumps({"dango": version, "docker_access": access})


def _record_lines(version: str, access: str) -> str:
    tmp = f"{RECORD_PATH}.tmp"
    return (
        f"printf '%s\\n' {shlex.quote(_record(version, access))} >{tmp}"
        f" && chown root:root {tmp} && chmod 0644 {tmp} && mv -f {tmp} {RECORD_PATH}\n"
    )


def record_script(version: str, access: str) -> str:
    """Replace the root-owned version record atomically."""
    return "# step:REC\nset -e\numask 022\n" + _record_lines(version, access)


def marker_script(version: str, access: str) -> str:
    """Write the version record, then the marker (root-owned, atomically): the marker comes last."""
    tmp = f"{ch.MARKER}.tmp"
    return (
        "# step:M\nset -e\numask 022\n"
        + _record_lines(version, access)
        + f": >{tmp} && chown root:root {tmp} && chmod 0644 {tmp} && mv -f {tmp} {ch.MARKER}\n"
    )


def prechecks_script() -> str:
    """Tools, Compose v2 and 1.0.14 server code that the helper needs."""
    tools = "sudo flock visudo runuser loginctl timeout getent gpasswd usermod pgrep pkill"
    return (
        "# step:H1\n"
        f"for c in {tools}; do\n"
        '  command -v "$c" >/dev/null 2>&1 || { echo "missing tool: $c" >&2; exit 1; }\n'
        "done\n"
        "docker compose version --short >/dev/null 2>&1 || "
        '{ echo "Docker Compose v2 is missing" >&2; exit 1; }\n'
        f"cd / && {VENV_PYTHON} -I -c 'import dango.platform.cloud.server_compose' "
        ">/dev/null 2>&1 || "
        '{ echo "the Dango installed on the server is older than 1.0.14" >&2; exit 1; }\n'
    )


def self_test_script() -> str:
    """As the service account: the helper runs through sudo, and a root-only verb is refused."""
    helper = f"sudo -n {ch.HELPER_PATH}"
    return (
        "# step:H3\n"
        "cd /\n"
        f'out=$(runuser -u dango -- {helper} self-test 2>&1) || {{ echo "$out" >&2; exit 1; }}\n'
        '[ "$out" = ok ] || { echo "helper self-test said: $out" >&2; exit 1; }\n'
        f"runuser -u dango -- {helper} volume-path >/dev/null 2>&1\n"
        'rc=$?; [ "$rc" -eq 64 ] || { echo "a root-only helper verb was not refused (exit $rc)" >&2; exit 1; }\n'
    )


def render_script() -> str:
    """Render the server compose file through the helper, as the service account."""
    return (
        "# step:P2\ncd / && runuser -u dango -- env DANGO_CLOUD_MODE=true "
        f"{VENV_PYTHON} -I -m dango.platform.compose_exec render --helper {PROJECT_DIR}\n"
    )


def verify_script() -> str:
    """As the service account: the helper can list the project's containers."""
    return f"# step:P3\ncd / && runuser -u dango -- sudo -n {ch.HELPER_PATH} ps >/dev/null\n"


def consistency_script(project_id: str) -> str:
    """Check the pinned name against what exists on the host (a consistency check, not a boundary).

    Containers of this project must already use the name the id gives, an existing Metabase volume
    must be a plain ``local`` volume (no driver options) whose label, if any, names this id.
    """
    if not ID_RE.fullmatch(project_id):
        raise ValueError("bad project id")
    proj = f"dango-{project_id[:8]}"
    vol = f"{proj}_metabase-data"
    label = '{{.Label "com.docker.compose.project"}}'
    return (
        f"# step:P1\nproj={proj}; id={project_id}; vol={vol}\n"
        "bad=$(docker ps -a --filter "
        f"label=com.docker.compose.project.working_dir={PROJECT_DIR} "
        f"--format '{label}' | sort -u | grep -vx \"$proj\" || true)\n"
        'if [ -n "$bad" ]; then echo "containers of this project use the compose name $bad, '
        'project.yml gives $proj" >&2; exit 2; fi\n'
        'if docker volume inspect "$vol" >/dev/null 2>&1; then\n'
        '  info=$(docker volume inspect "$vol" --format '
        "'{{.Driver}}|{{len .Options}}|{{index .Labels \"com.dango.project_id\"}}')\n"
        "  drv=${info%%|*}; rest=${info#*|}; nopt=${rest%%|*}; lab=${rest#*|}\n"
        '  [ "$drv" = local ] && [ "$nopt" = 0 ] || '
        '{ echo "volume $vol is not a plain local volume ($drv, $nopt options)" >&2; exit 2; }\n'
        '  case "$lab" in ""|"<no value>"|"$id") ;; '
        '*) echo "volume $vol belongs to project $lab, project.yml gives $id" >&2; exit 2;; esac\n'
        "fi\n"
        f"cur=$(cat {ch.COMPOSE_DIR}/project-id 2>/dev/null || true)\n"
        'if [ -z "$cur" ]; then echo PINNED=none; elif [ "$cur" = "$id" ]; then echo PINNED=same; '
        'else echo "PINNED=other:$cur"; fi\n'
        "docker ps -a --format '{{.Names}} {{.Label \"com.docker.compose.project\"}}' | "
        'while read -r name p; do case "$name" in dango-*) '
        '[ "$p" = "$proj" ] || echo "WARN:container $name belongs to another compose project";; esac; done\n'
        "true\n"
    )


def drop_group_script() -> str:
    """Remove the service account from the docker group; fail if it is still a member."""
    member = "id -nG dango | tr ' ' '\\n' | grep -qx docker"
    return (
        "# step:G\n"
        f"if {member}; then gpasswd -d dango docker >/dev/null || exit 1; fi\n"
        f'if {member}; then echo "dango is still in the docker group" >&2; exit 1; fi\n'
    )


def terminate_script() -> str:
    """End dango processes that still hold the docker gid (groups are fixed at process start).

    Waits up to 120 s for other dango processes (such as a manual sync), then up to three rounds of
    ``loginctl terminate-user``, TERM, a 10 s wait, STOP and KILL.  Succeeds (``T_OK``) only when no
    live process of the account has the docker gid in its ``/proc/<pid>/status`` Groups line.
    """
    awk = (
        '/^State:/{if($2=="Z")z=1} /^Uid:/{if($2==u)m=1} '
        "/^Groups:/{for(i=2;i<=NF;i++)if($i==g)h=1} END{if(m&&h&&!z)exit 0; exit 1}"
    )
    return (
        "# step:T\n"
        "dg=$(getent group docker | cut -d: -f3)\n"
        "du=$(id -u dango) || { echo T_OK; exit 0; }\n"
        "w=0\n"
        'while pgrep -u dango >/dev/null 2>&1 && [ "$w" -lt 120 ]; do sleep 2; w=$((w + 2)); done\n'
        "if pgrep -u dango >/dev/null 2>&1; then\n"
        "  echo \"T_LEFT:$(ps -u dango -o args= 2>/dev/null | head -5 | tr '\\n' ';')\"\n"
        "fi\n"
        "has_gid() {\n"
        "  for s in /proc/[0-9]*/status; do\n"
        '    [ -r "$s" ] || continue\n'
        f'    awk -v u="$du" -v g="$dg" \'{awk}\' "$s" && return 0\n'
        "  done\n"
        "  return 1\n"
        "}\n"
        "r=0\n"
        'while [ "$r" -lt 3 ] && has_gid; do\n'
        "  r=$((r + 1))\n"
        "  loginctl terminate-user dango >/dev/null 2>&1\n"
        "  pkill -TERM -u dango >/dev/null 2>&1\n"
        "  n=0\n"
        '  while [ "$n" -lt 10 ] && pgrep -u dango >/dev/null 2>&1; do sleep 1; n=$((n + 1)); done\n'
        "  pkill -STOP -u dango >/dev/null 2>&1\n"
        "  pkill -KILL -u dango >/dev/null 2>&1\n"
        "  sleep 1\n"
        "done\n"
        "if has_gid; then echo T_REMAIN; exit 3; fi\n"
        "echo T_OK\n"
    )


def fallback_script(version: str) -> str:
    """Back to group access: remove the marker, then add the group if the account lacks it."""
    return (
        "# step:FB\nset -e\numask 022\n"
        f"rm -f {ch.MARKER}\n"
        "if ! id -nG dango | tr ' ' '\\n' | grep -qx docker; then usermod -aG docker dango; fi\n"
        + _record_lines(version, ACCESS_GROUP)
    )


def restore_script(version: str) -> str:
    """Downgrade below 1.0.14: group access first, the Metabase volume back to the old uid, then
    remove the marker and the sudoers rule, then record the target version with ``group``."""
    sudoers = ch.SUDOERS_PATH
    return (
        "# step:R\nset -e\numask 022\n"
        "if ! id -nG dango | tr ' ' '\\n' | grep -qx docker; then\n"
        '  usermod -aG docker dango || { echo "could not add dango to the docker group" >&2; exit 1; }\n'
        "fi\n"
        f"proj=$(cat {ch.COMPOSE_DIR}/project 2>/dev/null || true)\n"
        'if [ -n "$proj" ]; then\n'
        '  vol="${proj}_metabase-data"\n'
        '  if docker volume inspect "$vol" >/dev/null 2>&1; then\n'
        '    info=$(docker volume inspect "$vol" --format '
        "'{{.Driver}}|{{len .Options}}|{{.Mountpoint}}')\n"
        "    drv=${info%%|*}; rest=${info#*|}; nopt=${rest%%|*}; mp=${rest#*|}\n"
        '    if [ "$drv" = local ] && [ "$nopt" = 0 ] && [ -d "$mp" ] && [ ! -L "$mp" ] && '
        'case "$mp" in /*/volumes/"$vol"/_data) true;; *) false;; esac; then\n'
        f'      chown -hR {OLD_METABASE_ID}:{OLD_METABASE_ID} "$mp" || '
        f'echo "RESTORE_WARN:could not return $vol to uid {OLD_METABASE_ID}"\n'
        "    else\n"
        '      echo "RESTORE_WARN:$vol was not changed: not a plain local volume"\n'
        "    fi\n"
        "  fi\n"
        "fi\n"
        f"rm -f {ch.MARKER}\n"
        f"rm -f {sudoers}\n" + _record_lines(version, ACCESS_GROUP)
    )


def ownership_steps() -> list[OwnershipStep]:
    """Extra ``secure_runtime`` step: helper, sudoers rule and compose dir stay root-owned."""
    paths = f"{ch.COMPOSE_DIR} {ch.HELPER_PATH} {ch.SUDOERS_PATH} {ch.METABASE_DIR}"
    script = (
        f"for p in {paths}; do\n"
        '  if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi\n'
        "done\n"
        f"test ! -d {ch.COMPOSE_DIR} || {{ chown -R root:root {ch.COMPOSE_DIR}"
        f" && chmod -R go-w {ch.COMPOSE_DIR}; }}\n"
        f"test ! -e {ch.HELPER_PATH} || {{ chown root:root {ch.HELPER_PATH}"
        f" && chmod 0755 {ch.HELPER_PATH}; }}\n"
        f"test ! -e {ch.SUDOERS_PATH} || {{ chown root:root {ch.SUDOERS_PATH}"
        f" && chmod 0440 {ch.SUDOERS_PATH}; }}\n"
        f"test ! -d {ch.METABASE_DIR} || {{ chown root:root {ch.METABASE_DIR}"
        f" && chmod 0700 {ch.METABASE_DIR}; }}\n"
        "bad=$( {\n"
        f"  test ! -d {ch.COMPOSE_DIR} || find {ch.COMPOSE_DIR} \\( ! -user root -o -perm /022 \\) -print\n"
        f"  for p in {ch.HELPER_PATH} {ch.SUDOERS_PATH}; do test ! -e $p ||"
        " find $p -maxdepth 0 \\( ! -user root -o -perm /022 \\) -print; done\n"
        "} 2>&1 | head -20 )\n"
        'test -z "$bad" || { echo "$bad"; exit 1; }'
    )
    return [OwnershipStep("compose_helper", script)]
