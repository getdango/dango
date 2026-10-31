"""dango/platform/cloud/runner_isolation.py

Plan and policy for turning on restricted runner users on a cloud server.

Holds the ordered step list as pure data (what to run, as whom, how a failure is treated),
the id rules for the runner users, the marker record and how it is read back, and
:func:`run_steps`, which applies the failure policy to results supplied by an executor.
Importing this module has no side effects.  :func:`enable` runs the plan over SSH for server
setup and ``remote upgrade``; the command line (opt-out, status) comes later.

Failure policy: the marker is the last step and is never reached after a fatal failure. A failed
``nftables`` or ``acl`` package install, or failed firewall rules, only means ``loopback:
unrestricted`` plus a warning, as does the read-only report of skipped notebook directories;
everything else about files (users, sudoers, deny ACLs, notebook ACLs, verification) is fatal.
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from dango.logging import get_logger
from dango.platform.cloud import isolation_status as iso
from dango.platform.cloud import runner_access_scripts as access
from dango.platform.cloud import runner_persistence as persistence
from dango.platform.cloud import runner_scripts as scripts
from dango.platform.run_as import MARKER, PYTHON, RESERVED_UIDS

__all__ = [
    "MARKER",
    "Outcome",
    "RunnerRecord",
    "Step",
    "StepResult",
    "ISOLATION_MARK",
    "RESTART_NOTE",
    "assert_runner_ids",
    "build_steps",
    "enable",
    "enable_in_upgrade",
    "executor",
    "failure_detail",
    "parse_record",
    "read_record",
    "run_steps",
    "status_line",
]

_logger = get_logger(__name__)

RunAs = Literal["root", "service"]
OnFailure = Literal["fatal", "warn"]

#: ``loopback`` values in the record.
RESTRICTED = scripts.LOOPBACK_RESTRICTED
UNRESTRICTED = scripts.LOOPBACK_UNRESTRICTED
_TRUSTED_UID = 0


@dataclass(frozen=True)
class Step:
    """One named, idempotent script of the enabling sequence."""

    name: str
    script: str
    run_as: RunAs = "root"
    on_failure: OnFailure = "fatal"
    #: Steps that must have succeeded; otherwise this one is not attempted.
    requires: tuple[str, ...] = ()
    #: Text added to the outcome's warnings when a ``warn`` step fails.
    warning: str = ""

    @property
    def command(self) -> str:
        """What to give a root shell: the script, or the script wrapped to run as the service user."""
        if self.run_as == "service":
            return scripts.as_service_user(self.script)
        return self.script


@dataclass(frozen=True)
class StepResult:
    """What an executor reports for one step."""

    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class Outcome:
    """Result of applying the plan."""

    enabled: bool
    loopback: str = UNRESTRICTED
    warnings: tuple[str, ...] = ()
    failed: str | None = None
    detail: str = ""
    attempted: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class RunnerRecord:
    """Content of the marker file."""

    schema: int
    loopback: str


NFT_WARNING = (
    "Firewall rules for the runner users were not installed (loopback: unrestricted): "
    "scripts and notebooks can still reach Dango's local web services, this host's own "
    "addresses and the Docker containers (Metabase)."
)
ACL_WARNING = "The acl package could not be installed."
NFT_PACKAGE_WARNING = "The nftables package could not be installed."


def assert_runner_ids(users: dict[str, tuple[int, int]]) -> None:
    """Check ``{name: (uid, gid)}``: ids in 1000-59999, none reserved, uids distinct.

    Raises:
        ValueError: On the first violation.
    """
    uids = []
    for name, (uid, gid) in users.items():
        for kind, value in (("uid", uid), ("gid", gid)):
            if value in RESERVED_UIDS or not scripts.UID_MIN <= value < scripts.UID_MAX:
                raise ValueError(
                    f"runner {name}: {kind} {value} is not allowed (must be "
                    f"{scripts.UID_MIN}-{scripts.UID_MAX - 1} and not in {sorted(RESERVED_UIDS)})"
                )
        uids.append(uid)
    if len(set(uids)) != len(uids):
        raise ValueError(f"runner users must have distinct uids, got {uids}")


def build_steps(srv_dir: str = scripts.SRV_DIR) -> list[Step]:
    """The enabling sequence, in order; the marker is last."""
    return [
        # root acts below srv_dir only after it is known that the service account cannot swap it
        Step("ownership_check", access.ownership_precheck_script(srv_dir)),
        # nothing may run as a runner while permissions are written
        Step("quiesce", access.quiesce_script(stop_web=True)),
        Step(
            "acl_package",
            scripts.package_script("acl", "setfacl"),
            on_failure="warn",
            warning=ACL_WARNING,
        ),
        Step(
            "nftables_package",
            scripts.package_script("nftables", "nft"),
            on_failure="warn",
            warning=NFT_PACKAGE_WARNING,
        ),
        Step("runner_base_dir", scripts.runner_base_dir_script(srv_dir)),
        Step("users", scripts.users_script(srv_dir)),
        # a runner must not be able to start code later through cron, at or systemd
        Step("persistence", persistence.persistence_script()),
        Step("runner_dirs", scripts.runner_dirs_script(srv_dir)),
        Step("sudoers", scripts.sudoers_install_script()),
        Step(
            "project_permissions",
            access.project_permission_script(srv_dir=srv_dir),
            run_as="service",
        ),
        Step("notebooks_acl", access.notebooks_acl_script(srv_dir=srv_dir), run_as="service"),
        Step(
            "notebooks_report",
            access.notebooks_report_script(srv_dir=srv_dir),
            run_as="service",
            on_failure="warn",
            warning=access.NOTEBOOKS_SKIPPED_WARNING,
        ),
        Step(
            "nft_rules",
            scripts.nft_install_script(),
            on_failure="warn",
            requires=("nftables_package",),
            warning=NFT_WARNING,
        ),
        Step("verify", access.verify_script(srv_dir=srv_dir)),
        Step("marker", scripts.marker_script(srv_dir), requires=("verify",)),
    ]


def run_steps(steps: Sequence[Step], execute: Callable[[Step], StepResult]) -> Outcome:
    """Apply *steps* in order through *execute* and the failure policy.

    A fatal failure ends the run at once (so the marker is never written); a ``warn``
    failure adds a warning, and steps that require it are not attempted.
    """
    warnings: list[str] = []
    done: set[str] = set()
    tried: list[str] = []
    loopback = UNRESTRICTED
    for step in steps:
        if not set(step.requires) <= done:
            if step.on_failure == "fatal":
                return Outcome(
                    False,
                    loopback,
                    tuple(warnings),
                    step.name,
                    "a required step failed",
                    tuple(tried),
                )
            if step.warning and step.warning not in warnings:
                warnings.append(step.warning)
            continue
        tried.append(step.name)
        result = execute(step)
        if result.ok:
            done.add(step.name)
            if step.name == "nft_rules":
                loopback = RESTRICTED
            continue
        if step.on_failure == "fatal":
            return Outcome(False, loopback, tuple(warnings), step.name, result.detail, tuple(tried))
        warnings.append(f"{step.warning} ({result.detail})" if result.detail else step.warning)
    return Outcome(True, loopback, tuple(warnings), None, "", tuple(tried))


def parse_record(text: str) -> RunnerRecord:
    """Parse the marker content.

    Raises:
        ValueError: If it is not the expected JSON object.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"marker is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("marker is not a JSON object")
    schema, loopback = data.get("schema"), data.get("loopback")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ValueError("marker schema is not a positive integer")
    if loopback not in (RESTRICTED, UNRESTRICTED):
        raise ValueError(f"marker loopback is not recognised: {loopback!r}")
    return RunnerRecord(schema, loopback)


def read_record(path: str = MARKER, *, trusted_uid: int = _TRUSTED_UID) -> RunnerRecord | None:
    """Read the marker as :func:`dango.platform.run_as.uses_isolation` trusts it.

    Returns ``None`` unless the path is a regular file (not a link) owned by *trusted_uid*
    that no group or other user can write, and its content parses.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        trusted = (
            stat.S_ISREG(st.st_mode)
            and st.st_uid == trusted_uid
            and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        )
        if not trusted:
            return None
        with os.fdopen(fd, encoding="utf-8", errors="replace") as handle:
            fd = -1
            text = handle.read(4096)
    finally:
        if fd != -1:
            os.close(fd)
    try:
        return parse_record(text)
    except ValueError:
        return None


# --- Running the plan over SSH ---

#: Start of the loud warning when the steps could not be completed.
ISOLATION_MARK = "Runner isolation is NOT enabled"
_RETRY_HINT = "Run `dango remote upgrade` to retry once the cause is fixed."
#: SSH timeout per step; package installs may wait for apt.
_PACKAGE_TIMEOUT = 300
_STEP_TIMEOUT = 120
_RECORD_KEY = "runner_isolation"


def record_python(trusted_uid: int = _TRUSTED_UID) -> str:
    """Python source that copies the marker's schema and loopback into the version record.

    Both files must be regular, owned by *trusted_uid* and not group/other writable.  Without a
    marker the key is dropped, so a record never claims an isolation the server does not have.
    """
    return (
        "import json, os, stat, sys\n"
        "m, r = sys.argv[1], sys.argv[2]\n"
        "def ok(p):\n"
        "    s = os.lstat(p)\n"
        f"    return stat.S_ISREG(s.st_mode) and s.st_uid == {trusted_uid} and not s.st_mode & 0o022\n"
        "has = os.path.lexists(m)\n"
        "if (has and not ok(m)) or not ok(r):\n"
        "    sys.exit(0)\n"
        "d = json.load(open(r))\n"
        "if has:\n"
        "    mk = json.load(open(m))\n"
        f"    d['{_RECORD_KEY}'] = {{'schema': mk['schema'], 'loopback': mk['loopback']}}\n"
        "else:\n"
        f"    d.pop('{_RECORD_KEY}', None)\n"
        "t = r + '.tmp'\n"
        "open(t, 'w').write(json.dumps(d) + '\\n')\n"
        f"os.chown(t, {trusted_uid}, -1)\n"
        "os.chmod(t, 0o644)\n"
        "os.replace(t, r)\n"
    )


def record_script(srv_dir: str = scripts.SRV_DIR) -> str:
    """Merge the marker's schema and loopback state into ``server-version.json`` (non-secret)."""
    return (
        f"cd / && python3 -I -c {shlex.quote(record_python())} "
        f"{shlex.quote(scripts.marker_path(srv_dir))} {shlex.quote(srv_dir + '/server-version.json')}\n"
    )


def failure_detail(stderr: Any, stdout: Any, exit_code: Any) -> str:
    """Last line of a failed command's error output (or its exit code)."""
    text = str(stderr or stdout or "").strip().splitlines()
    return text[-1][:200] if text else f"exit {exit_code}"


def executor(ssh: Any, on_step: Callable[[], None] | None) -> Callable[[Step], StepResult]:
    """Executor for :func:`run_steps` that runs each step as root over *ssh* (*on_step* after a success)."""

    def execute(step: Step) -> StepResult:
        """Run one step as root over SSH; a failure is the last line of its error output."""
        timeout = _PACKAGE_TIMEOUT if step.name.endswith("_package") else _STEP_TIMEOUT
        # the tag lets a fake or a log tell the steps apart; it is a comment to the shell
        command = f"# runner-step:{step.name}\n{step.command}"
        try:
            result = ssh.exec_command(f"bash -c {shlex.quote(command)}", timeout=timeout)
        except Exception as exc:  # a dropped connection is a failed step, not a crash
            return StepResult(False, type(exc).__name__)
        if result.success:
            if on_step is not None:
                on_step()
            return StepResult(True)
        return StepResult(False, failure_detail(result.stderr, result.stdout, result.exit_code))

    return execute


def status_line(outcome: Outcome) -> str:
    """One line for the upgrade result."""
    if not outcome.enabled:
        return f"{ISOLATION_MARK}: {outcome.failed} failed ({outcome.detail or 'no detail'})"
    if outcome.loopback == RESTRICTED:
        return "Runner isolation enabled"
    return "Runner isolation enabled (loopback: unrestricted)"


_WEB_UNIT = "dango-web"
#: Added to the result when the steps stopped a running ``dango-web`` and started it again.
RESTART_NOTE = (
    "dango-web was stopped and started again, so open notebooks and running scripts were ended"
)
# looks for the file only: root never imports code from a venv that may not be trusted yet
_BUILD_CHECK = (
    f"find {posixpath.dirname(posixpath.dirname(PYTHON))}/lib -maxdepth 5 "
    "-path '*/site-packages/dango/runtime/isolated_run.py' -print 2>/dev/null | grep -q ."
)
BUILD_MISSING = "the Dango build installed on the server does not include it"


def build_present(ssh: Any) -> bool:
    """True when the server's venv has the isolated-run entry point (the build supports isolation)."""
    try:
        return bool(ssh.exec_command(_BUILD_CHECK, timeout=60).success)
    except Exception:  # a dropped connection: let the steps report it
        return True


def web_is_active(ssh: Any) -> bool:
    """True when ``dango-web`` is running (so it has to be started again after the steps)."""
    try:
        return bool(ssh.exec_command(f"systemctl is-active --quiet {_WEB_UNIT}").success)
    except Exception:  # unknown: leave the service alone
        return False


def start_web(ssh: Any, warnings: list[str]) -> None:
    """Start ``dango-web`` again after the steps stopped it; a failure is a warning."""
    try:
        ok = bool(ssh.exec_command(f"systemctl start {_WEB_UNIT}", timeout=120).success)
    except Exception:
        ok = False
    if not ok:
        warnings.append(f"{_WEB_UNIT} could not be started again. Run `dango remote upgrade`.")


def _isolation_state(ssh: Any, srv_dir: str) -> str:
    """``disabled`` (opt-out marker), ``on`` (active marker) or ``missing``."""
    if iso.disabled_marker_present(ssh, srv_dir):
        return "disabled"
    try:
        test = f"test -f {shlex.quote(scripts.marker_path(srv_dir))}"
        return "on" if ssh.exec_command(test).success else "missing"
    except Exception:
        return "missing"


def _failure_warning(outcome: Outcome, state: str) -> str:
    """The loud warning, worded by what is in force: ``on``, ``disabled`` or ``missing``."""
    cause = f"{outcome.failed}: {outcome.detail or 'failed'}"
    wording = {
        "on": "Isolation from an earlier run is still in force (scripts and notebooks stay "
        "isolated or are refused), but this run did not finish",
        "disabled": "Isolation is disabled by an admin opt-out; scripts and notebooks run as the service account",
    }
    state = wording.get(
        state, "Scripts and notebooks keep running as the service account, which is less isolated"
    )
    return f"{ISOLATION_MARK} ({cause}). {state}. {_RETRY_HINT}"


def enable(
    ssh: Any,
    warnings: list[str],
    *,
    on_step: Callable[[], None] | None = None,
    srv_dir: str = scripts.SRV_DIR,
) -> str:
    """Run the plan on the server; returns the status line, warnings go to *warnings*.

    Every step is idempotent and the marker is last, so an interrupted run converges when
    repeated.  A fatal failure leaves the marker as it was and adds a loud warning; a ``warn``
    step only adds its warning.  *on_step* is called after each successful step (the guard
    re-arm).  The plan stops ``dango-web`` and ends all runner processes before it writes
    permissions, then starts the service again if it was running.  Nothing runs on a build
    without ``isolated_run``.
    """
    if not build_present(ssh):
        warnings.append(f"{ISOLATION_MARK} ({BUILD_MISSING}). {_RETRY_HINT}")
        return f"{ISOLATION_MARK}: {BUILD_MISSING}"
    was_active = web_is_active(ssh)
    try:
        outcome = run_steps(build_steps(srv_dir), executor(ssh, on_step))
        warnings.extend(w for w in outcome.warnings if w not in warnings)
        if not outcome.enabled:
            warnings.append(_failure_warning(outcome, _isolation_state(ssh, srv_dir)))
        # with a marker the record shows it; without one a stale key is dropped.  Never when the
        # ownership check failed: root must not write under a directory that is not trusted.
        if outcome.failed != "ownership_check":
            result = executor(ssh, None)(Step("record", record_script(srv_dir), on_failure="warn"))
            if not result.ok:
                _logger.warning("runner_isolation_record_failed", detail=result.detail)
    finally:
        if was_active:
            start_web(ssh, warnings)
    status = status_line(outcome)
    if was_active and "quiesce" in outcome.attempted:
        status = f"{status}; {RESTART_NOTE}"
    if outcome.enabled and iso.disabled_marker_present(ssh, srv_dir):
        warnings.append(iso.DISABLED_WARNING)
        return iso.DISABLED_LINE
    return status


def enable_in_upgrade(
    ssh: Any,
    warnings: list[str],
    version: str | None,
    guard: Any,
    *,
    own_guard: bool,
) -> str | None:
    """:func:`enable` for ``remote upgrade``, after Docker access is settled.

    Below 1.0.14 nothing is enabled: the marker is taken away instead (``isolation_downgrade``).
    The guard is re-armed after every step.  On the at-target path no guard is armed yet
    (*own_guard*): one is armed around the steps.
    """
    from dango.platform.cloud.compose_versions import below_helper_version, helper_capable
    from dango.platform.cloud.isolation_downgrade import demote

    if below_helper_version(version):
        return demote(ssh, warnings, version or "", guard, own_guard=own_guard)
    if not helper_capable(version):
        return None
    if own_guard:
        guard.arm()
    try:
        return enable(ssh, warnings, on_step=guard.rearm)
    finally:
        if own_guard:
            guard.disarm()
