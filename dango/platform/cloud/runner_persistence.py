"""dango/platform/cloud/runner_persistence.py

Keep a runner user from persisting code outside ``dango-web``'s control group.

A locked ``nologin`` account can still install a crontab or an ``at`` job (PAM does not refuse
locked accounts), and a lingering systemd user or an autostart entry would restart code the same
way.  Such a process is not killed when the web service stops, so it could race the permission
steps.  This module builds the shell text that (1) denies both runners cron and ``at``, (2) clears
what they may already have left behind, and (3) checks that it holds.  Pure text, root-side.

Root never follows a link a runner controls: files are removed by exact name, and the runner
homes are searched with ``find -P`` (links are listed, never entered), so a planted link is
unlinked and its target is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass

from dango.platform.cloud.runner_scripts import RUNNER_USERS, q

__all__ = [
    "DEFAULT_PATHS",
    "PersistencePaths",
    "cleanup_after_kill",
    "cleanup_before_kill",
    "deny_lists_script",
    "persistence_checks",
    "persistence_script",
    "ssh_deny_script",
]

_USERS = " ".join(RUNNER_USERS)
_DENY_FILES = ("cron.deny", "at.deny")
#: Relative to a runner home: what a nologin user could use to start code on its own.
_HOME_UNITS = (
    ".ssh",
    ".config/autostart",
    ".config/environment.d",
    ".config/systemd",
    ".local/share/systemd",
)
#: Directories a runner could replace with a link to a tree it controls (systemd follows it).
_HOME_LINKS = (".config", ".local", ".local/share")
SSH_DROP_IN = "dango-runner.conf"
_SSHD_FIND = "SSHD=$(command -v sshd || echo /usr/sbin/sshd)"
_SSHD_PORT = (
    # the first configured port (Match blocks are not evaluated without ``-C``); 22 when unusable
    'SP=$("$SSHD" -T 2>/dev/null | awk \'$1 == "port" {print $2; exit}\') || SP=\n'
    "case \"$SP\" in ''|*[!0-9]*|??????*) SP=22;; esac\n"
    '[ "$SP" -ge 1 ] && [ "$SP" -le 65535 ] || SP=22'
)
#: Connection spec for ``sshd -T -C``: a ``Match LocalPort``/``LocalAddress`` block needs both.
_SSHD_SPEC = "user=$u,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=$SP"
_DENY_RE = '"^denyusers( .*)? $u( |\\$)"'
_LINK_TESTS = " -o ".join(f'-path "$h/{d}"' for d in _HOME_LINKS)


@dataclass(frozen=True)
class PersistencePaths:
    """System locations the cleanup and the checks look at (overridable for tests)."""

    linger_dir: str = "/var/lib/systemd/linger"
    cron_spool: str = "/var/spool/cron/crontabs"
    at_spool: str = "/var/spool/cron/atjobs"


DEFAULT_PATHS = PersistencePaths()

_AT_JOBS = "atq 2>/dev/null | awk -v u=\"$u\" '$NF == u {print $1}'"


def deny_lists_script(etc: str = "/etc") -> str:
    """Add both runners to ``cron.deny`` and ``at.deny`` (created 0644 when absent), idempotently.

    Existing entries are kept; a missing final newline is added before appending.  A symlink or
    a non-regular file at either path is refused (exit 3), so nothing is written through a link.
    """
    return (
        "\n".join(
            [
                "set -eu",
                f"E={q(etc)}",
                f"for f in {' '.join(_DENY_FILES)}; do",
                '  p="$E/$f"',
                '  if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi',
                '  if [ -e "$p" ] && [ ! -f "$p" ]; then'
                ' echo "refusing: $p is not a regular file" >&2; exit 3; fi',
                '  [ -e "$p" ] || (umask 022; : >"$p")',
                '  [ ! -s "$p" ] || [ -z "$(tail -c 1 "$p")" ] || echo >>"$p"',
                f"  for u in {_USERS}; do",
                '    grep -qxF "$u" "$p" || echo "$u" >>"$p"',
                "  done",
                "done",
            ]
        )
        + "\n"
    )


def ssh_deny_script(sshd_dir: str = "/etc/ssh/sshd_config.d") -> str:
    """Root-side: refuse ssh logins of both runners and mask their systemd user managers.

    A locked ``nologin`` account can still log in with a key planted in its own home, and the
    login starts ``user@<uid>.service``, which runs units the runner planted outside
    ``dango-web``.  A ``DenyUsers`` drop-in naming only the two runners is written atomically
    (root:root 0644), checked with ``sshd -t`` (the previous file comes back when it fails),
    reloaded, and then proven effective with ``sshd -T -C`` for the local address and the
    configured port (so ``Match LocalPort`` blocks can be evaluated).  A missing ``Include``
    fails the step with 6, an ``sshd -T`` that cannot evaluate the config with 7: isolation is
    NOT enabled in both cases.  Fixed system paths only; a symlink or non-regular file at the
    drop-in path is refused.
    """
    names = " ".join(RUNNER_USERS)
    return (
        "\n".join(
            [
                "set -eu",
                f'D={q(sshd_dir)}; F="$D/{SSH_DROP_IN}"',
                _SSHD_FIND,
                'new=; bak=; trap \'rm -f -- "$new" "$bak"\' EXIT',
                '[ -d "$D" ] || mkdir -p -m 0755 "$D"',
                'if [ -L "$F" ] || { [ -e "$F" ] && [ ! -f "$F" ]; }; then'
                ' echo "refusing: $F is a link or not a regular file" >&2; exit 3; fi',
                'new=$(mktemp "$D/.dango-runner.XXXXXX")',
                'if [ -f "$F" ]; then bak=$(mktemp "$D/.dango-runner-old.XXXXXX"); cp -p "$F" "$bak"; fi',
                "printf '%s\\n' '# Installed by Dango: the runner users never log in over ssh.'"
                f" 'DenyUsers {names}' >\"$new\"",
                'chmod 0644 "$new"; chown root:root "$new"',
                'mv -f "$new" "$F"',
                'if ! "$SSHD" -t; then',
                '  if [ -n "$bak" ]; then mv -f "$bak" "$F"; else rm -f "$F"; fi',
                '  echo "sshd rejected the config, restored it; isolation NOT enabled" >&2; exit 4',
                "fi",
                "systemctl reload ssh || systemctl reload sshd || {"
                ' echo "could not reload sshd; isolation NOT enabled" >&2; exit 5; }',
                _SSHD_PORT,
                f"for u in {names}; do",
                f'  out=$("$SSHD" -T -C "{_SSHD_SPEC}" 2>&1) || {{',
                '    echo "sshd -T could not evaluate the config for $u ($(printf %s "$out" | head -n 1));'
                ' isolation NOT enabled" >&2; exit 7; }',
                f'  printf %s "$out" | grep -Eq {_DENY_RE} || {{',
                "    echo \"sshd does not apply $F (add 'Include /etc/ssh/sshd_config.d/*.conf' to"
                ' sshd_config); isolation NOT enabled" >&2; exit 6; }',
                '  systemctl mask "user@$(id -u "$u").service" >/dev/null',
                '  systemctl stop "user@$(id -u "$u").service" >/dev/null 2>&1 || true',
                "done",
            ]
        )
        + "\n"
    )


def persistence_script() -> str:
    """The ``persistence`` setup step: cron/at deny lists, then the ssh and user-manager denial."""
    return deny_lists_script() + ssh_deny_script()


def _jobs_cleanup(paths: PersistencePaths) -> str:
    """Per-user (``$u``) removal of crontab, at jobs and lingering; used inside a ``for u`` loop."""
    return "\n".join(
        [
            f"S={q(paths.cron_spool)}; A={q(paths.at_spool)}; L={q(paths.linger_dir)}",
            '  crontab -r -u "$u" >/dev/null 2>&1 || true',
            '  rm -f "$S/$u"',
            "  if command -v atq >/dev/null 2>&1; then",
            f'    for j in $({_AT_JOBS}); do atrm "$j" >/dev/null 2>&1 || true; done',
            "  fi",
            '  [ ! -d "$A" ] || find -P "$A" -maxdepth 1 -type f -user "$u" -delete 2>/dev/null || true',
            '  loginctl disable-linger "$u" >/dev/null 2>&1 || true',
            '  rm -f "$L/$u"',
            '  systemctl stop "user@$(id -u "$u").service" >/dev/null 2>&1 || true',
            '  loginctl terminate-user "$u" >/dev/null 2>&1 || true',
        ]
    )


def _home_cleanup() -> str:
    """Remove user units and autostart entries from a runner home (``$u`` set), never following links."""
    prunes = " -o ".join(f'-path "$h/{unit}"' for unit in _HOME_UNITS)
    return "\n".join(
        [
            '  h=$(getent passwd "$u" | cut -d: -f6)',
            '  if [ -n "$h" ]; then',
            # planted links first: find -P never enters them, so the prune pass cannot reach what they hide
            f'    find -P "$h" -mindepth 1 -maxdepth 2 -type l \\( {_LINK_TESTS} \\) -delete'
            " 2>/dev/null || true",
            f'    find -P "$h" -mindepth 1 -maxdepth 3 \\( {prunes} \\) -prune -exec rm -rf {{}} +'
            " 2>/dev/null || true",
            "  fi",
        ]
    )


def cleanup_before_kill(paths: PersistencePaths = DEFAULT_PATHS) -> str:
    """Shell lines (a ``for`` over both runners): drop crontab, at jobs and lingering first."""
    return "\n".join(
        [
            f"for u in {_USERS}; do",
            '  getent passwd "$u" >/dev/null 2>&1 || continue',
            "  " + _jobs_cleanup(paths),
            "done",
        ]
    )


def cleanup_after_kill(paths: PersistencePaths = DEFAULT_PATHS) -> str:
    """Shell lines run once no runner process exists: repeat the job cleanup, clear the home entries."""
    return "\n".join(
        [
            f"for u in {_USERS}; do",
            '  getent passwd "$u" >/dev/null 2>&1 || continue',
            "  " + _jobs_cleanup(paths),
            _home_cleanup(),
            "done",
        ]
    )


def persistence_checks(etc: str = "/etc", paths: PersistencePaths = DEFAULT_PATHS) -> str:
    """Verify fragment (root; needs the caller's ``fail``): the runners cannot persist code.

    Fails when ``crontab -l`` as a runner is not refused, when a crontab file exists, when a runner
    is missing from ``cron.deny`` or ``at.deny``, when an ``at`` job can be queued, when systemd
    lingering is on, or when a user unit or autostart entry is present in its home.
    """
    unit_tests = " -o ".join(f'-path "$h/{unit}"' for unit in _HOME_UNITS)
    return (
        "\n".join(
            [
                f"E={q(etc)}; S={q(paths.cron_spool)}; L={q(paths.linger_dir)}",
                _SSHD_FIND,
                _SSHD_PORT,
                f"for u in {_USERS}; do",
                '  getent passwd "$u" >/dev/null 2>&1 || continue',
                "  if command -v crontab >/dev/null 2>&1; then",
                '    out=$(runuser -u "$u" -- crontab -l 2>&1 || true)',
                '    case "$out" in *"not allowed"*) ;; *)'
                ' fail "$u can use crontab (check $E/cron.allow)";; esac',
                "  fi",
                '  [ ! -e "$S/$u" ] || fail "$u has a crontab"',
                f"  for f in {' '.join(_DENY_FILES)}; do",
                '    grep -qxF "$u" "$E/$f" 2>/dev/null || fail "$u is not in $E/$f"',
                "  done",
                "  if command -v at >/dev/null 2>&1; then",
                '    if echo true | runuser -u "$u" -- at now + 1 minute >/dev/null 2>&1; then',
                '      fail "$u can schedule at jobs"',
                f'      for j in $({_AT_JOBS}); do atrm "$j" >/dev/null 2>&1 || true; done',
                "    fi",
                "  fi",
                '  [ ! -e "$L/$u" ] || fail "$u has systemd lingering enabled"',
                '  uid=$(id -u "$u")',
                '  [ "$(systemctl is-enabled "user@$uid.service" 2>/dev/null)" = masked ]'
                ' || fail "user@$uid.service is not masked for $u"',
                f'  if out=$("$SSHD" -T -C "{_SSHD_SPEC}" 2>&1); then',
                f'    printf %s "$out" | grep -Eq {_DENY_RE} || fail "sshd does not deny ssh logins for $u"',
                '  else fail "sshd -T could not evaluate the config for $u"; fi',
                '  h=$(getent passwd "$u" | cut -d: -f6)',
                '  if [ -n "$h" ]; then',
                # find -P lists a link without entering it: a planted link is found, never followed
                f'    x=$(find -P "$h" -mindepth 1 -maxdepth 3 \\( {unit_tests} -o'
                f" \\( -type l \\( {_LINK_TESTS} \\) \\) \\) -print 2>/dev/null | head -n 1)",
                '    [ -z "$x" ] || fail "$u has $x"',
                "  fi",
                "done",
            ]
        )
        + "\n"
    )
