"""dango/platform/cloud/runtime_ownership.py

Root-owned runtime on cloud servers.

Everything root executes, imports or loads as environment must not be writable
by the ``dango`` service account: ``/srv/dango`` and the venv are root-owned,
the project stays ``dango``'s. The ownership script is a list of named steps so
later hardening (other service users, ACLs, helpers) can append its own.
Also holds the in-place rebuild of a venv that a non-root user could write
(project packages come from :mod:`dango.platform.cloud.pushed_requirements`).
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dango.exceptions import CloudProvisioningError
from dango.logging import get_logger
from dango.platform.cloud.pushed_requirements import install_pushed_requirements

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_logger = get_logger(__name__)

SRV_DIR = "/srv/dango"
VENV_DIR = f"{SRV_DIR}/venv"
PROJECT_DIR = f"{SRV_DIR}/project"
SECRETS_DIR = f"{SRV_DIR}/secrets"
BACKUPS_DIR = f"{SRV_DIR}/backups"
BACKUP_UNIT_PATH = "/etc/systemd/system/dango-backup.service"
LOGROTATE_PATH = "/etc/logrotate.d/dango"
#: Root runs the venv interpreter isolated and from ``/``: no cwd/script dir on
#: sys.path, no PYTHON* variables. Prefix for a command line.
ROOT_PYTHON = f"cd / && {VENV_DIR}/bin/python -I"
ROOT_UNITS = (
    "/etc/systemd/system/dango-web.service",
    BACKUP_UNIT_PATH,
    "/etc/systemd/system/dango-backup.timer",
    LOGROTATE_PATH,
)
VERSION_RE = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")

# Not owned by root, or group/other-writable (links are judged by their target).
_UNTRUSTED = r"\( ! -user root -o \( ! -type l \( -perm -020 -o -perm -002 \) \) \)"


def links_outside_script(root: str) -> str:
    """Shell that prints each symlink under *root* that resolves outside it and ``/usr``,
    or into ``/usr`` at something another user could write (*root* itself is checked
    separately)."""
    return (
        f'find {root} -type l -print0 2>/dev/null | while IFS= read -r -d "" l; do '
        't=$(readlink -f "$l"); '
        f'case "$t" in ({root}|{root}/*) continue;; (/usr/*) ;; (*) echo "$l -> $t"; continue;; esac; '
        'if [ -e "$t" ] && [ -n "$(find "$t" -maxdepth 0 \\( ! -user root -o \\( -perm -020 -o -perm -002 \\) \\) '
        '-print)" ]; then echo "$l -> $t"; fi; done'
    )


PROBE_SENTINEL = "DANGO_PROBE_OK"
# Findings (paths) then the sentinel; a missing sentinel or non-zero exit means untrusted.
VENV_TAINT_PROBE = (
    "set -o pipefail; out=$( { "
    f"find / /srv {SRV_DIR} -maxdepth 0 \\( -type l -o ! -user root -o "
    "\\( -perm -020 -o -perm -002 \\) \\) -print; "
    f"test ! -d {VENV_DIR} || {{ find {VENV_DIR} {_UNTRUSTED} -print -quit; "
    f"{links_outside_script(VENV_DIR)}; }}; }} 2>&1 | sed -n 1,5p ) || exit 2; "
    f'test -z "$out" || printf "%s\\n" "$out"; echo {PROBE_SENTINEL}'
)


class VenvRebuildError(CloudProvisioningError):
    """The venv could not be rebuilt as root-owned; the previous one was put back."""


@dataclass(frozen=True)
class OwnershipStep:
    """One named, idempotent shell fragment of the ownership script (runs as root)."""

    name: str
    script: str


def ownership_steps() -> list[OwnershipStep]:
    """Return the base ownership steps, in order."""
    guarded = " ".join((VENV_DIR, SECRETS_DIR, BACKUPS_DIR, f"{BACKUPS_DIR}/deploy", PROJECT_DIR))
    return [
        OwnershipStep(
            "symlink_guard",
            f"for p in {guarded}; do "
            'if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi; done',
        ),
        OwnershipStep(
            "srv_dango",
            f"install -d -o root -g root -m 0755 {SRV_DIR}"
            f" && chown root:root {SRV_DIR} && chmod 0755 {SRV_DIR}"
            f" && {{ ! command -v setfacl >/dev/null 2>&1 || setfacl -k {SRV_DIR}; }}",
        ),
        OwnershipStep(
            "venv",
            f"test ! -d {VENV_DIR}"
            f" || {{ chown -R root:root {VENV_DIR} && chmod -R go-w {VENV_DIR}; }}",
        ),
        OwnershipStep("secrets", f"install -d -o dango -g dango -m 0700 {SECRETS_DIR}"),
        OwnershipStep(
            "backups",
            f"install -d -m 0750 {BACKUPS_DIR} {BACKUPS_DIR}/deploy"
            f" && chown -R root:root {BACKUPS_DIR}"
            f" && chgrp -h dango {BACKUPS_DIR} {BACKUPS_DIR}/deploy"
            f" && chmod 0750 {BACKUPS_DIR} {BACKUPS_DIR}/deploy"
            f" && find {BACKUPS_DIR} -type f -exec chmod 0600 {{}} +",
        ),
        OwnershipStep("project", f"test ! -d {PROJECT_DIR} || chown -R dango:dango {PROJECT_DIR}"),
    ]


def build_ownership_script(extra_steps: Sequence[OwnershipStep] = ()) -> str:
    """Render the base steps plus *extra_steps* as one ``set -e`` script."""
    lines = ["set -e"]
    for step in [*ownership_steps(), *extra_steps]:
        lines.append(f"# {step.name}")
        lines.append(step.script)
    return "\n".join(lines) + "\n"


def build_verify_script() -> str:
    """Script printing every root-execution path another user could change; exits 1 if any.

    Covers ``/``, ``/srv``, ``/srv/dango``, the venv (and where its links point),
    the Dango systemd units and the logrotate file.
    """
    units = " ".join(ROOT_UNITS)
    return (
        f"bad=$( {{ find / /srv {SRV_DIR} -maxdepth 0 \\( -type l -o ! -user root -o \\( -perm -020 -o -perm -002 \\) \\) "
        "-print; "
        f"test ! -d {VENV_DIR} || {{ find {VENV_DIR} {_UNTRUSTED} -print; "
        f"{links_outside_script(VENV_DIR)}; }}; "
        f"for u in {units}; do test ! -e $u || find $u -maxdepth 0 \\( -type l -o "
        f"! -user root -o \\( -perm -020 -o -perm -002 \\) \\) -print; done; "
        '} 2>&1 | head -20 ); test -z "$bad" || { echo "$bad"; exit 1; }'
    )


def _run_script(ssh: SSHManager, script: str, timeout: int = 300) -> tuple[bool, str]:
    result = ssh.exec_command(f"bash -c {shlex.quote(script)}", timeout=timeout)
    return result.success, (result.stderr.strip() or result.stdout.strip())


def apply_ownership(
    ssh: SSHManager,
    warnings: list[str],
    extra_steps: Sequence[OwnershipStep] = (),
    *,
    strict: bool = False,
) -> bool:
    """Run the ownership script, then verify; returns success.

    Never takes an untrusted venv: rebuild it first (:func:`rebuild_untrusted_venv`).
    With *strict* (servers Dango provisioned), a failed check raises; otherwise
    (BYOS) it becomes a warning with the remediation.

    Raises:
        VenvRebuildError: The venv is untrusted (nothing was changed).
        CloudProvisioningError: *strict* and the script or the check failed.
    """
    if venv_is_untrusted(ssh):
        raise VenvRebuildError("the venv can be written by a non-root user; rebuild it first")
    ok, detail = _run_script(ssh, build_ownership_script(extra_steps))
    if ok:
        ok, detail = _run_script(ssh, build_verify_script(), timeout=120)
    if ok:
        return True
    message = (
        "Runtime ownership check failed; these paths are not root-owned or are writable by "
        f"other users: {detail}. Fix with `chown root:root <path> && chmod go-w <path>` "
        "(and replace any symlink listed), then re-run."
    )
    if strict:
        raise CloudProvisioningError(message)
    warnings.append(message)
    return False


def installed_version_from_metadata(ssh: SSHManager) -> str | None:
    """Read the installed getdango version from the venv's metadata, as data.

    Used instead of running the venv's interpreter when that venv cannot be
    trusted. The value is validated strictly because it is put in a command line.
    """
    result = ssh.exec_command(
        f"ls -d {VENV_DIR}/lib/python3*/site-packages/getdango-*.dist-info 2>/dev/null"
    )
    for line in result.stdout.splitlines():
        name = line.strip().rsplit("/", 1)[-1]
        version = name[len("getdango-") : -len(".dist-info")]
        if name.startswith("getdango-") and VERSION_RE.match(version):
            return version
    return None


REBUILD_ABORT = (
    "The venv could not be rebuilt as root-owned; upgrade aborted, server unchanged ({}). "
    "Retry `dango remote upgrade` or see the server logs."
)


def valid_version(value: str | None) -> str | None:
    """*value* if it is a plain release version string, else ``None`` (it reaches shells)."""
    return value if value and VERSION_RE.match(value) else None


def trusted_metadata_version(ssh: SSHManager, requested: str | None) -> str | None:
    """The version an untrusted venv records, believed only if the client knows it."""
    import dango

    found = installed_version_from_metadata(ssh)
    return found if found in {dango.__version__, requested} else None


def lock_srv_dango(ssh: SSHManager) -> None:
    """Make ``/srv/dango`` root-owned first, so nothing can swap the venv underneath.

    Raises:
        VenvRebuildError: The lock failed.
    """
    srv_step = next(step for step in ownership_steps() if step.name == "srv_dango")
    ok, detail = _run_script(ssh, srv_step.script)
    if not ok:
        raise VenvRebuildError(f"{SRV_DIR} could not be made root-owned ({detail})")


def venv_trust_findings(ssh: SSHManager) -> list[str]:
    """Paths that make the venv untrusted: ``/``, ``/srv`` or ``/srv/dango`` not root-owned,
    writable by others or a link; anything in the venv another user could write; a venv
    link leading out of it. A probe that does not finish counts as a finding."""
    result = ssh.exec_command(f"bash -c {shlex.quote(VENV_TAINT_PROBE)}", timeout=120)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not result.success or not lines or lines[-1] != PROBE_SENTINEL:
        return ["(the trust check did not complete)"]
    return lines[:-1]


def venv_is_untrusted(ssh: SSHManager) -> bool:
    """True if :func:`venv_trust_findings` finds anything."""
    return bool(venv_trust_findings(ssh))


def _remediation(ssh: SSHManager, findings: list[str]) -> str:
    """Name the fix for each finding (and a default ACL on /srv/dango, if any)."""
    paths = [f.split(" -> ", 1)[0] for f in findings if f.startswith("/")]
    fixes = [f"`chown root:root {p} && chmod go-w {p}`" for p in paths]
    acl = ssh.exec_command(f"getfacl -p {SRV_DIR} 2>/dev/null | grep '^default:'")
    if "default:" in acl.stdout:
        fixes.append(f"a default ACL on {SRV_DIR} grants access: `setfacl -b {SRV_DIR}`")
    return f"{'; '.join(findings)}. Fix: " + ("; ".join(fixes) or "see the server logs")


def at_target_restart_plan(ssh: SSHManager) -> str | None:
    """What an at-target ``remote upgrade`` would restart, or ``None`` if nothing.

    Only a venv rebuild restarts services; ownership and config refreshes do not.
    """
    if not venv_is_untrusted(ssh):
        return None
    return (
        "Plan: rebuild the server venv as root-owned at the same version (a non-root "
        "user can write it). Services stop and restart."
    )


def refresh_root_config(
    ssh: SSHManager, warnings: list[str], installed_version: str | None
) -> None:
    """Rewrite the backup unit (if present) for *installed_version* and the logrotate config."""
    from dango.platform.cloud._server_templates import LOGROTATE_CONF, backup_service_unit

    changed = False
    if ssh.exec_command(f"test -f {BACKUP_UNIT_PATH}").success:
        unit, unit_warning = backup_service_unit(installed_version)
        if unit_warning:
            warnings.append(unit_warning)
        ssh.write_remote_file(BACKUP_UNIT_PATH, unit, mode=0o644)
        changed = True
    if ssh.exec_command(f"test -f {LOGROTATE_PATH}").success:
        ssh.write_remote_file(LOGROTATE_PATH, LOGROTATE_CONF, mode=0o644)
    if changed and not ssh.exec_command("systemctl daemon-reload", timeout=60).success:
        warnings.append("systemctl daemon-reload failed after updating dango-backup.service")


# ---------------------------------------------------------------------------
# In-place rebuild of a venv a non-root user could write
# ---------------------------------------------------------------------------


def rebuild_venv(
    ssh: SSHManager,
    warnings: list[str],
    *,
    version: str | None,
    local_project_root: Path | None = None,
) -> str:
    """Replace the venv with a fresh root-owned one at the same path.

    The venv is rebuilt at its final path (console-script shebangs embed it): the
    old one is moved aside, a new one created and verified, then the old removed.
    Call with services stopped. Returns a status line.

    Raises:
        VenvRebuildError: The rebuild failed; the previous venv was put back.
    """
    if not version or not VERSION_RE.match(version):
        raise VenvRebuildError(f"no valid getdango version to install ({version!r})")
    aside = f"{VENV_DIR}.untrusted-{uuid.uuid4().hex[:12]}"
    if not ssh.exec_command(f"mv {VENV_DIR} {aside}").success:
        raise VenvRebuildError("the current venv could not be moved aside")
    pip = f"{VENV_DIR}/bin/pip"
    steps = [
        (f"python3 -m venv {VENV_DIR}", 120),
        (f"{pip} install --no-input --upgrade pip -q", 300),
        (f"{pip} install --no-input getdango=={version} -q", 600),
    ]
    failed = next(
        (cmd for cmd, timeout in steps if not ssh.exec_command(cmd, timeout=timeout).success), None
    )
    verified = (
        failed is None
        and ssh.exec_command(
            f'{ROOT_PYTHON} -c "import dango; print(dango.__version__)"', timeout=60
        ).success
    )
    if verified:
        ssh.exec_command(f"{ROOT_PYTHON} -m spacy download en_core_web_sm -q", timeout=180)
        if local_project_root is not None:
            install_pushed_requirements(ssh, local_project_root, pip, warnings)
        else:
            warnings.append(
                "The venv was rebuilt without project packages; run `dango remote push` to "
                "install requirements.txt."
            )
        # Trust the new venv only if nothing in it, or behind its links, is writable by others.
        findings = venv_trust_findings(ssh)
        if findings:
            failed = f"the post-rebuild check: {_remediation(ssh, findings)}"
            verified = False
    if not verified:
        restored = ssh.exec_command(f"rm -rf {VENV_DIR} && mv {aside} {VENV_DIR}").success
        where = "the previous venv was put back" if restored else f"the previous venv is at {aside}"
        raise VenvRebuildError(f"{failed or 'the import check'} failed; {where}")
    ssh.exec_command(f"rm -rf {aside}", timeout=300)
    return f"venv rebuilt as root (getdango=={version})"


def rebuild_untrusted_venv(
    ssh: SSHManager,
    warnings: list[str],
    *,
    version: str | None,
    local_project_root: Path | None = None,
) -> str | None:
    """Rebuild the venv at *version*, with services stopped, if a non-root user could write it.

    Run before root executes anything from the venv. *version* must come from the
    client (target or local version), never from the server's metadata alone.
    Services are stopped and started with systemctl/compose only. Returns a status
    line, or ``None`` if the venv was trusted.

    Raises:
        VenvRebuildError: The venv could not be checked or rebuilt (fail closed);
            the previous venv is back in place and services were started again.
    """
    from dango.platform.cloud.backup import start_services, stop_services

    try:
        findings = venv_trust_findings(ssh)
    except Exception as exc:
        raise VenvRebuildError(f"the venv could not be checked ({type(exc).__name__})") from exc
    if not findings:
        return None
    parents = [f for f in findings if f in ("/", "/srv")]
    if parents:  # outside what Dango manages: refuse rather than change the host
        raise VenvRebuildError(_remediation(ssh, parents))
    stop_services(ssh)
    try:
        lock_srv_dango(ssh)
        return rebuild_venv(ssh, warnings, version=version, local_project_root=local_project_root)
    except VenvRebuildError:
        raise
    except Exception as exc:
        raise VenvRebuildError(f"unexpected {type(exc).__name__}") from exc
    finally:
        start_services(ssh)


def secure_runtime(
    ssh: SSHManager,
    warnings: list[str],
    *,
    installed_version: str | None,
    strict: bool = False,
    extra_steps: Sequence[OwnershipStep] = (),
) -> str:
    """Apply ownership and refresh root-run config; returns a status line.

    Run :func:`rebuild_untrusted_venv` first. Unexpected errors become *warnings*;
    an untrusted venv, or a failed check with *strict*, raises
    ``CloudProvisioningError`` (``VenvRebuildError`` for the venv).
    """
    try:
        ok = apply_ownership(ssh, warnings, extra_steps, strict=strict)
        refresh_root_config(ssh, warnings, installed_version)
    except CloudProvisioningError:
        raise
    except Exception as exc:
        warnings.append(f"Runtime ownership step failed ({type(exc).__name__}).")
        return f"Runtime ownership skipped: {type(exc).__name__}"
    return "Runtime ownership applied" if ok else "Runtime ownership incomplete (see warnings)"
