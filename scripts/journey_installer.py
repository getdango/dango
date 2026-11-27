"""scripts/journey_installer.py

The first-user journey's `installer` step (--installer): run install.sh (POSIX) or install.ps1
(Windows) from the checkout, non-interactively, with the built wheel as DANGO_INSTALL_SPEC and a
clean directory as its working directory, then point the journey at the dango and python the
installer put in its virtual environment. Standard library only.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from journey_support import Journey, Step, failure

CHECKOUT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALLER_TIMEOUT_SECONDS = 600

# Answers for every prompt the installer can show on this path (install only, virtual environment).
# The execution-policy answer is read by install.ps1 only, and only when the policy is too strict.
# A process-level execution policy of the shell that started the harness is not passed on.
INSTALLER_ENV_REMOVE = ("PSExecutionPolicyPreference",)
_DIST_SUFFIXES = (".whl", ".tar.gz", ".zip")

# Which getdango the venv holds: its version and, for a wheel or URL install, where pip got it.
_DIST_INFO = """
import json, importlib.metadata as m
d = m.distribution("getdango")
raw = d.read_text("direct_url.json")
print(json.dumps({"version": d.version, "direct_url": json.loads(raw) if raw else None}))
"""

INSTALLER_ANSWERS = {
    "DANGO_INSTALL_NONINTERACTIVE": "1",
    "DANGO_INSTALL_DOCKER_CONTINUE": "y",
    "DANGO_INSTALL_MODE": "1",
    "DANGO_INSTALL_SETUP": "1",
    "DANGO_INSTALL_ALLOW_EXECUTION_POLICY": "y",
}


def installer_command(script: Path | None) -> list[str]:
    """Command that runs the installer script (the checkout's own, unless `script` is given)."""
    if script is None:
        script = CHECKOUT / ("install.ps1" if sys.platform == "win32" else "install.sh")
    if script.suffix == ".ps1":
        # Run it the way the documented one-liner does: its UTF-8 text through iex, with no
        # execution-policy option. The installer ends an iex run with its exit code in
        # $LASTEXITCODE (it never calls `exit` there), so hand that code on.
        quoted = str(script).replace("'", "''")
        text = f"[System.IO.File]::ReadAllText('{quoted}', [System.Text.Encoding]::UTF8)"
        shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [shell, "-NoProfile", "-Command", f"iex ({text}); exit $LASTEXITCODE"]
    return ["bash", str(script)]


def _same_file(url: str, spec: str) -> bool:
    """True when the file:// `url` pip recorded points at the `spec` path."""
    parsed = urlparse(url)
    if parsed.scheme != "file":
        return False
    recorded = os.path.normcase(os.path.realpath(url2pathname(parsed.path)))
    return recorded == os.path.normcase(os.path.realpath(spec))


def check_installed_from(spec: str, info: dict[str, object]) -> tuple[str | None, bool]:
    """(why the installed getdango does not come from `spec` or None, whether the source was checked).

    A `name==version` spec is checked by version; a wheel or sdist path by the file URL pip
    recorded in direct_url.json; a URL by that recorded URL. Other specs (a bare name, a range)
    are not verifiable: no problem is reported and the second value is False.
    """
    direct = info.get("direct_url")
    url = str(direct.get("url", "")) if isinstance(direct, dict) else ""
    if "==" in spec and "://" not in spec and not spec.lower().endswith(_DIST_SUFFIXES):
        wanted = spec.split("==", 1)[1].strip()
        if info.get("version") != wanted:
            return f"installed version is {info.get('version')}, wanted {wanted}", True
    elif "://" in spec:
        if not url or url.split("#")[0] not in spec:
            return f"pip recorded {url or 'no source URL (not a direct install)'}", True
    elif os.path.exists(spec) or spec.lower().endswith(_DIST_SUFFIXES):
        if not _same_file(url, spec):
            return f"pip recorded {url or 'no source URL (not a direct install)'}", True
    else:
        return None, False
    return None, True


def step_installer(
    j: Journey, spec: str, installer_dir: Path, script: Path | None, timeout: float
) -> Step:
    """Run the installer in `installer_dir`, then make the journey use the dango it installed."""
    installer_dir.mkdir(parents=True, exist_ok=True)
    env_extra = {**INSTALLER_ANSWERS, "DANGO_INSTALL_SPEC": spec}
    proc = j.run(
        installer_command(script),
        timeout=timeout,
        cwd=installer_dir,
        env_extra=env_extra,
        env_remove=INSTALLER_ENV_REMOVE,
    )
    if proc.error is not None:
        return failure("installer", f"installer: {proc.error}", proc.output)
    if proc.returncode != 0:
        return failure("installer", f"installer: exit code {proc.returncode}", proc.output)
    # A good install shows no pip error: a Windows pip that is asked to replace its own pip.exe
    # prints one in the middle of an install that goes on to succeed.
    error_line = next((ln for ln in proc.output.splitlines() if "ERROR:" in ln), None)
    if error_line is not None:
        reason = f"installer printed an error although it exited 0: {error_line.strip()}"
        return failure("installer", reason, proc.output)
    windows = sys.platform == "win32"
    bin_dir = installer_dir / "venv" / ("Scripts" if windows else "bin")
    dango = bin_dir / ("dango.exe" if windows else "dango")
    python = bin_dir / ("python.exe" if windows else "python")
    for exe in (dango, python):
        if not exe.is_file():
            return failure("installer", f"the installer left no {exe}", proc.output)
    j.dango, j.python = str(dango), str(python)
    version = j.run([j.dango, "--version"], cwd=installer_dir)
    if version.error is not None or version.returncode != 0:
        detail = version.error or f"exit code {version.returncode}"
        return failure("installer", f"installed dango --version: {detail}", version.output)
    dist = j.run([j.python, "-c", _DIST_INFO], cwd=installer_dir)
    try:
        info = json.loads(dist.output.strip().splitlines()[-1]) if dist.returncode == 0 else None
    except (ValueError, IndexError):
        info = None
    if not isinstance(info, dict):
        return failure("installer", "could not read the installed getdango's metadata", dist.output)
    problem, verified = check_installed_from(spec, info)
    if problem:
        return failure("installer", f"installed getdango does not come from {spec}: {problem}")
    note = "" if verified else " (source not verified)"
    return Step("installer", "PASS", f"installed getdango {info.get('version')} from {spec}{note}")
