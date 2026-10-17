"""dango/platform/cloud/runtime_ownership.py

Root-owned runtime on cloud servers.

Everything root executes, imports or loads as environment must not be writable
by the ``dango`` service account: ``/srv/dango`` and the venv are root-owned,
the project stays ``dango``'s. The ownership script is a list of named steps so
later hardening (other service users, ACLs, helpers) can append its own.
Also holds the rules for installing pushed ``requirements.txt`` files as root,
and the in-place rebuild of a venv that a non-root user could write.
"""

from __future__ import annotations

import re
import shlex
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dango.logging import get_logger

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
#: Root runs the venv interpreter isolated: no cwd/script dir on sys.path, no PYTHON* vars.
ROOT_PYTHON = f"{VENV_DIR}/bin/python -I"

# Anything in the venv not owned by root, or group/other-writable (links excluded).
_UNTRUSTED = r"\( ! -user root -o \( ! -type l -perm /022 \) \)"
VENV_TAINT_PROBE = (
    f"{{ find {SRV_DIR} -maxdepth 0 ! -user root -print; "
    f"test ! -d {VENV_DIR} || find {VENV_DIR} {_UNTRUSTED} -print -quit; }} 2>/dev/null"
)


@dataclass(frozen=True)
class OwnershipStep:
    """One named, idempotent shell fragment of the ownership script (runs as root)."""

    name: str
    script: str


def ownership_steps() -> list[OwnershipStep]:
    """Return the base ownership steps, in order."""
    guarded = " ".join((VENV_DIR, SECRETS_DIR, BACKUPS_DIR, PROJECT_DIR))
    return [
        OwnershipStep(
            "symlink_guard",
            f"for p in {guarded}; do "
            'if [ -L "$p" ]; then echo "refusing: $p is a symlink" >&2; exit 3; fi; done',
        ),
        OwnershipStep(
            "srv_dango",
            f"install -d -o root -g root -m 0755 {SRV_DIR}"
            f" && chown root:root {SRV_DIR} && chmod 0755 {SRV_DIR}",
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
            f" && chgrp dango {BACKUPS_DIR} {BACKUPS_DIR}/deploy"
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
    """Script that prints every root-execution path not root-owned or group/other-writable.

    Exits non-zero if it printed anything.
    """
    units = f"{BACKUP_UNIT_PATH} /etc/systemd/system/dango-web.service"
    return (
        f"bad=$( {{ find / /srv {SRV_DIR} -maxdepth 0 {_UNTRUSTED} -print; "
        f"test ! -d {VENV_DIR} || find {VENV_DIR} {_UNTRUSTED} -print; "
        f"for u in {units}; do test ! -e $u || find $u {_UNTRUSTED} -print; done; "
        '} 2>&1 | head -20 ); test -z "$bad" || { echo "$bad"; exit 1; }'
    )


def _run_script(ssh: SSHManager, script: str, timeout: int = 300) -> tuple[bool, str]:
    result = ssh.exec_command(f"bash -c {shlex.quote(script)}", timeout=timeout)
    return result.success, (result.stderr.strip() or result.stdout.strip())


def apply_ownership(
    ssh: SSHManager, warnings: list[str], extra_steps: Sequence[OwnershipStep] = ()
) -> bool:
    """Run the ownership script, then verify. Problems become *warnings*; returns success."""
    ok, detail = _run_script(ssh, build_ownership_script(extra_steps))
    if not ok:
        warnings.append(f"Runtime ownership was not applied: {detail}")
        return False
    ok, detail = _run_script(ssh, build_verify_script(), timeout=120)
    if not ok:
        warnings.append(
            "Runtime ownership check failed; these paths are not root-owned or are "
            f"writable by other users: {detail}"
        )
        return False
    return True


_VERSION_RE = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


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
        if name.startswith("getdango-") and _VERSION_RE.match(version):
            return version
    return None


def lock_srv_dango(ssh: SSHManager) -> None:
    """Make ``/srv/dango`` root-owned first, so nothing can swap the venv underneath."""
    srv_step = next(step for step in ownership_steps() if step.name == "srv_dango")
    _run_script(ssh, srv_step.script)


def venv_is_untrusted(ssh: SSHManager) -> bool:
    """True if ``/srv/dango`` or anything in the venv could be written by a non-root user."""
    result = ssh.exec_command(VENV_TAINT_PROBE, timeout=120)
    return bool(result.stdout.strip())


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
# Requirements installed by root
# ---------------------------------------------------------------------------

REQUIREMENT_FILES = ("requirements.txt", "scripts/requirements.txt")
_NESTED_OR_EDITABLE = re.compile(
    r"^(-e|--editable|-r|--requirement|-c|--constraint)(\s|=|$)|^-[erc]\S", re.I
)
_URL_OPTION = re.compile(r"^(-i|--index-url|--extra-index-url|-f|--find-links)(?:\s+|=)(\S+)", re.I)
_URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s@]+@", re.I)
_SSH_GIT_USER = re.compile(r"^[a-z+]*ssh://git@", re.I)
_LOCAL_ARCHIVE = re.compile(r"\.(whl|tar\.gz|zip|tgz|tar\.bz2)$", re.I)


def _strip_comment(line: str) -> str:
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()


def _redact(line: str) -> str:
    return re.sub(r"(://)[^/\s@]+@", r"\1***@", line)


def check_requirements(text: str) -> list[str]:
    """Return the lines root must not install (credentials redacted); ``[]`` if all are fine.

    Refused: editable installs, nested ``-r``/``-c`` files, ``file:`` and local or
    relative paths, local archives, and URLs carrying credentials.
    """
    refused: list[str] = []
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if not line:
            continue
        url_option = _URL_OPTION.match(line)
        if url_option:
            target = url_option.group(2)
            if "://" not in target or _URL_CREDENTIALS.search(target):
                refused.append(_redact(line))
            continue
        spec = line.split(";", 1)[0].split(" --hash", 1)[0].strip()
        url = spec.split("@", 1)[1].strip() if " @ " in spec else spec
        local = (
            spec.startswith((".", "/", "~"))
            or "file:" in spec.lower()
            or ("://" not in url and ("/" in url or _LOCAL_ARCHIVE.search(url) is not None))
        )
        credentials = _URL_CREDENTIALS.search(spec) and not _SSH_GIT_USER.match(url)
        if _NESTED_OR_EDITABLE.match(line) or local or credentials:
            refused.append(_redact(line))
    return refused


def install_pushed_requirements(
    ssh: SSHManager,
    local_project_root: Path,
    pip: str,
    warnings: list[str],
) -> list[str]:
    """Install the local requirements files with *pip*, as root, from private uploads.

    The server's copies are never read: they are writable by ``dango``. Returns
    the labels of the files installed.
    """
    installed: list[str] = []
    for rel in REQUIREMENT_FILES:
        local = local_project_root / rel
        if not local.is_file():
            continue
        text = local.read_text(encoding="utf-8")
        refused = check_requirements(text)
        if refused:
            warnings.append(
                f"{rel} not installed on the server: root installs only index packages and "
                f"remote URLs without credentials; refused: {'; '.join(refused)}"
            )
            continue
        workdir = f"/tmp/dango-req-{uuid.uuid4().hex}"
        if not ssh.exec_command(f"mkdir -m 0700 {workdir}").success:
            warnings.append(f"{rel} not installed: could not create a private directory")
            continue
        try:
            remote = f"{workdir}/requirements.txt"
            ssh.write_remote_file(remote, text, mode=0o600)
            result = ssh.exec_command(f"{pip} install --no-input -r {remote}", timeout=300)
        finally:
            ssh.exec_command(f"rm -rf {workdir}")  # cleanup: silent OK
        if result.success:
            installed.append(rel)
            _logger.info("requirements_installed", path=rel)
        else:
            warnings.append(f"pip install -r {rel} failed: {result.stderr.strip()}")
    return installed


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
    """Replace the venv with a fresh root-owned one at the same path; keep the old on failure.

    The venv is rebuilt at its final path (console-script shebangs embed it): the
    old one is moved aside, a new one created and verified, then the old removed.
    Call with services stopped. Returns a status line.
    """
    if not version:
        warnings.append("venv not rebuilt: the installed Dango version is unknown")
        return "venv rebuild skipped: unknown version"
    aside = f"{VENV_DIR}.untrusted-{uuid.uuid4().hex[:12]}"
    if not ssh.exec_command(f"mv {VENV_DIR} {aside}").success:
        warnings.append("venv not rebuilt: could not move the current venv aside")
        return "venv rebuild skipped: move failed"
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
    if not verified:
        restored = ssh.exec_command(f"rm -rf {VENV_DIR} && mv {aside} {VENV_DIR}").success
        warnings.append(
            "The venv could be written by a non-root user and rebuilding it failed "
            f"({failed or 'import check'}); the previous venv was "
            + ("kept. Re-run `dango remote upgrade`." if restored else f"left at {aside}.")
        )
        return "venv rebuild failed: previous venv kept" if restored else "venv rebuild failed"
    ssh.exec_command(f"{ROOT_PYTHON} -m spacy download en_core_web_sm -q", timeout=180)
    if local_project_root is not None:
        install_pushed_requirements(ssh, local_project_root, pip, warnings)
    else:
        warnings.append(
            "The venv was rebuilt without project packages; run `dango remote push` to "
            "install requirements.txt."
        )
    ssh.exec_command(f"rm -rf {aside}", timeout=300)
    return f"venv rebuilt as root (getdango=={version})"


def rebuild_untrusted_venv(
    ssh: SSHManager, warnings: list[str], local_project_root: Path | None = None
) -> str | None:
    """Rebuild the venv, with services stopped, if a non-root user could write it.

    Run before root executes anything from the venv: the version comes from the
    venv metadata (data, not code). Services are always started again. Never
    raises; problems become *warnings*. Returns a status line, or ``None`` if the
    venv was trusted.
    """
    from dango.platform.cloud.backup import start_services, stop_services

    try:
        if not venv_is_untrusted(ssh):
            return None
        stop_services(ssh)
        try:
            lock_srv_dango(ssh)
            version = installed_version_from_metadata(ssh)
            return rebuild_venv(
                ssh, warnings, version=version, local_project_root=local_project_root
            )
        finally:
            start_services(ssh)
    except Exception as exc:
        warnings.append(f"venv rebuild step failed ({type(exc).__name__}).")
        return f"venv rebuild skipped: {type(exc).__name__}"


def secure_runtime(
    ssh: SSHManager,
    warnings: list[str],
    *,
    installed_version: str | None,
    local_project_root: Path | None = None,
    allow_rebuild: bool = True,
    extra_steps: Sequence[OwnershipStep] = (),
) -> tuple[str, str | None]:
    """Rebuild an untrusted venv (if *allow_rebuild*), apply ownership, refresh root config.

    A rebuild replaces the venv, so pass ``allow_rebuild=True`` only with services
    stopped. Never raises for server-side problems: they become *warnings*.
    Returns ``(ownership status, venv rebuild status or None)``.
    """
    rebuild_status: str | None = None
    try:
        if allow_rebuild and venv_is_untrusted(ssh):
            lock_srv_dango(ssh)
            rebuild_status = rebuild_venv(
                ssh, warnings, version=installed_version, local_project_root=local_project_root
            )
        ok = apply_ownership(ssh, warnings, extra_steps)
        refresh_root_config(ssh, warnings, installed_version)
    except Exception as exc:
        warnings.append(f"Runtime ownership step failed ({type(exc).__name__}).")
        return f"Runtime ownership skipped: {type(exc).__name__}", rebuild_status
    status = "Runtime ownership applied" if ok else "Runtime ownership incomplete (see warnings)"
    return status, rebuild_status
