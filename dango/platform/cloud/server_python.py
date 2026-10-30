"""dango/platform/cloud/server_python.py

The Python interpreter the server venv (``/srv/dango/venv``) is built from.

Dango needs Python 3.11-3.13 (``requires-python`` in ``pyproject.toml``); Ubuntu 22.04 ships
3.10.  A root-owned host interpreter in range is used as it is (Ubuntu 24.04: its own 3.12);
otherwise a pinned, checksum-verified CPython build is installed under ``/opt/dango/python``.
Used by fresh setup (DigitalOcean, BYOS, ``remote migrate``) and by ``remote upgrade``,
which ensures the interpreter before anything is stopped or changed.  The shell scripts and
pinned values are in :mod:`dango.platform.cloud.server_python_scripts`.
"""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from dango.exceptions import CloudError, CloudProvisioningError, CloudSSHError
from dango.platform.cloud import server_python_scripts as sps

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

ERROR_CODE = "DANGO-D024"
#: First getdango release that needs Python 3.11 or newer on the server.
FIRST_RELEASE = (1, 0, 14)
#: Interpreter for targets before :data:`FIRST_RELEASE` (they still run on 3.10).
LEGACY_PYTHON = "python3"
VENV_CFG = "/srv/dango/venv/pyvenv.cfg"

_PATH_RE = re.compile(r"/[A-Za-z0-9._+/-]+")
_PROBE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)\.(\w+)")
_NOVENV = "novenv "
#: SSH read timeout for the install script, which prints nothing until it ends: well above
#: ``sps.LOCK_WAIT + sps.DOWNLOAD_MAX_TIME`` plus the checks and extraction.
INSTALL_STEP_TIMEOUT = 1200

_CAUSES = {
    sps.EXIT_DOWNLOAD: f"the download from {sps.DOWNLOAD_HOSTS} failed",
    sps.EXIT_SIZE: "the downloaded file has the wrong size",
    sps.EXIT_CHECKSUM: "the downloaded file failed the SHA-256 check",
    sps.EXIT_ARCHIVE: "the downloaded archive failed the safety checks",
    sps.EXIT_LOCATION: "/opt/dango could not be used (it must be a root-owned directory "
    "that other users cannot write, not a symbolic link)",
    sps.EXIT_TOOLS: "a required tool is missing",
}


class ServerPythonError(CloudProvisioningError):
    """No Python 3.11-3.13 could be found or installed for the server venv."""

    _default_error_code = ERROR_CODE


def _refusal(cause: str) -> ServerPythonError:
    return ServerPythonError(
        f"The server has no usable Python {sps.SUPPORTED} for the Dango venv and Dango could "
        f"not install one: {cause}. Install Python {sps.SUPPORTED} on the server from the "
        f"operating system's packages (root-owned, under /usr, with its venv module), or "
        f"allow outbound HTTPS to {sps.DOWNLOAD_HOSTS}; then run the command again."
    )


def _detail(stderr: str, stdout: str) -> str:
    text = (stderr or stdout or "").strip().splitlines()
    return text[-1][:200] if text else ""


def _run_sh(ssh: SSHManager, script: str, timeout: int = 120) -> tuple[int, str, str]:
    result = ssh.exec_command(f"sh -c {shlex.quote(script)}", timeout=timeout)
    return result.exit_code, result.stdout, result.stderr


def _in_range(probe_output: str, *, version: str | None = None) -> bool:
    """True if *probe_output* (``PROBE``) names a final release in range (or exactly *version*)."""
    lines = probe_output.strip().splitlines()
    match = _PROBE_RE.fullmatch(lines[-1].strip()) if lines else None
    if not match or match.group(4) != "final":
        return False
    found = (int(match.group(1)), int(match.group(2)))
    if version is not None:
        return ".".join(match.group(1, 2, 3)) == version
    return sps.MIN_PYTHON <= found < sps.MAX_PYTHON


def host_probe(ssh: SSHManager) -> tuple[str | None, str | None]:
    """Host interpreters: ``(usable one, in-range one without venv)``; at most one is set.

    The first is the resolved path of a root-owned interpreter in range that the venv can be
    built from; the second only names one that lacks its venv module (for the message).
    """
    code, out, _ = _run_sh(ssh, sps.host_python_script())
    line = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
    novenv = line.startswith(_NOVENV)
    path = line[len(_NOVENV) :] if novenv else line
    if not (path and _PATH_RE.fullmatch(path) and "/../" not in f"{path}/"):
        return None, None
    return (None, path) if novenv else (path, None)


def server_arch(ssh: SSHManager) -> str:
    """The :data:`PYTHON_BUILDS` key for the server's CPU; refuses any other architecture."""
    result = ssh.exec_command("uname -m", timeout=30)
    machine = result.stdout.strip() if result.success else ""
    arch = sps.ARCHES.get(machine)
    if arch is None:
        raise _refusal(
            f"no pinned Python build for this CPU architecture ({machine or 'unknown'}; "
            "supported: x86_64, aarch64)"
        )
    return arch


def pinned_installed(ssh: SSHManager, build: sps.PythonBuild) -> bool:
    """True if the pinned interpreter is installed, root-owned, intact and runs."""
    code, out, _ = _run_sh(ssh, sps.installed_check_script(build.sha256))
    return code == 0 and _in_range(out, version=sps.PYTHON_VERSION)


def install_pinned(ssh: SSHManager, build: sps.PythonBuild) -> None:
    """Download, verify and install *build*; raises :class:`ServerPythonError` on failure."""
    code, out, err = _run_sh(ssh, sps.install_script(build), timeout=INSTALL_STEP_TIMEOUT)
    if code == 0:
        return
    cause = _CAUSES.get(code, f"the install failed (exit {code})")
    detail = _detail(err, out)
    raise _refusal(f"{cause} ({detail})" if detail else cause)


def ensure_server_python(ssh: SSHManager, *, warnings: list[str]) -> str:
    """Return the absolute path of an interpreter the server venv can be built from.

    Order: a root-owned host interpreter in range (nothing is downloaded), the pinned build
    already installed under ``/opt/dango/python``, else install the pinned build (download,
    size and SHA-256 check, safe extraction).  Idempotent.  Adds a line to *warnings* when
    it installs.

    Raises:
        ServerPythonError: No interpreter could be found or installed (``DANGO-D024``).
    """
    host, without_venv = host_probe(ssh)
    if host is not None:
        return host
    build = sps.PYTHON_BUILDS[server_arch(ssh)]
    if pinned_installed(ssh, build):
        return sps.PYTHON_BIN
    install_pinned(ssh, build)
    if not pinned_installed(ssh, build):
        raise _refusal(f"the installed interpreter at {sps.PYTHON_DIR} failed its check")
    why = (
        f"the server's {without_venv} has no venv/ensurepip module; installing its venv "
        "package (on Ubuntu: `apt install python3-venv`) avoids this download"
        if without_venv
        else f"the server had no usable root-owned Python {sps.SUPPORTED} with venv and ensurepip"
    )
    warnings.append(
        f"Installed Python {sps.PYTHON_VERSION} for the Dango venv at {sps.PYTHON_DIR} ({why})."
    )
    return sps.PYTHON_BIN


def needs_new_python(version: str | None) -> bool:
    """True if getdango *version* needs Python 3.11 or newer (1.0.14 and later)."""
    from packaging.version import InvalidVersion, Version

    if not version:
        return True
    try:
        return Version(version).release[:3] >= FIRST_RELEASE
    except InvalidVersion:
        return True


def venv_python_in_range(ssh: SSHManager) -> bool:
    """True if the venv's ``pyvenv.cfg`` records a Python in range (read as data, never run).

    Only for a root-owned (trusted) venv; a missing or unreadable record counts as out of
    range, so the venv is rebuilt.
    """
    result = ssh.exec_command(
        f"test -f {VENV_CFG} && test ! -L {VENV_CFG} && "
        "sed -n 's/^version[[:space:]]*=[[:space:]]*\\([0-9][0-9]*\\.[0-9][0-9]*\\).*/\\1/p' "
        f"{VENV_CFG} | head -n 1",
        timeout=30,
    )
    match = re.fullmatch(r"(\d+)\.(\d+)", result.stdout.strip()) if result.success else None
    if match is None:
        return False
    return sps.MIN_PYTHON <= (int(match.group(1)), int(match.group(2))) < sps.MAX_PYTHON


def prepare_upgrade(
    ssh: SSHManager,
    warnings: list[str],
    old_version: str | None,
    target_version: str,
    untrusted: bool,
) -> tuple[str, bool]:
    """Before ``remote upgrade`` stops or changes anything: ``(interpreter, venv_outdated)``.

    For a target before 1.0.14 nothing is checked (``python3``, not outdated).  Otherwise a
    trusted venv is outdated when its Python is out of range, and when the venv will be
    rebuilt (untrusted or outdated) the interpreter is ensured here, so a failure refuses
    the whole upgrade with nothing stopped or changed.  A trusted venv in range is upgraded
    in place by pip and needs nothing.

    Raises:
        CloudError: No interpreter, or the SSH command providing it failed (``DANGO-D024``);
            services were not stopped and Dango was not changed.
    """
    if not needs_new_python(target_version):
        return LEGACY_PYTHON, False
    outdated = not untrusted and not venv_python_in_range(ssh)
    if not (untrusted or outdated):
        return LEGACY_PYTHON, False
    try:
        return ensure_server_python(ssh, warnings=warnings), outdated
    except ServerPythonError as exc:
        raise _upgrade_refusal(target_version, old_version, str(exc)) from exc
    except CloudSSHError as exc:
        cause = (
            f"The SSH command that provides Python {sps.SUPPORTED} for the Dango venv failed "
            f"({exc}); an install may still be finishing on the server, so run the command "
            "again."
        )
        raise _upgrade_refusal(target_version, old_version, cause) from exc


def _upgrade_refusal(target_version: str, old_version: str | None, cause: str) -> CloudError:
    current = f"Dango {old_version}" if old_version else "the installed Dango"
    return CloudError(
        f"Upgrade to {target_version} refused: {cause} Services were not stopped and Dango "
        f"was not changed; {current} keeps running.",
        error_code=ERROR_CODE,
    )
