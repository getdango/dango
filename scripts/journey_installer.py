"""scripts/journey_installer.py

The first-user journey's `installer` step (--installer): run install.sh (POSIX) or install.ps1
(Windows) from the checkout, non-interactively, with the built wheel as DANGO_INSTALL_SPEC and a
clean directory as its working directory, then point the journey at the dango and python the
installer put in its virtual environment. Standard library only.
"""

import shutil
import sys
from pathlib import Path

from journey_support import Journey, Step, failure

CHECKOUT = Path(__file__).resolve().parents[1]
DEFAULT_INSTALLER_TIMEOUT_SECONDS = 600

# Answers for every prompt the installer can show on this path (install only, virtual environment).
# The execution-policy answer is read by install.ps1 only, and only when the policy is too strict.
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
        # Run it as UTF-8 text, as `irm https://getdango.dev/install.ps1 | iex` does. The file has
        # no BOM, so `powershell -File` (Windows PowerShell 5.1) reads it as ANSI and cannot parse it.
        # No -ExecutionPolicy: that sets a process-level policy that overrides the user-level one
        # the installer sets, and the one-liner has none.
        quoted = str(script).replace("'", "''")
        text = f"[System.IO.File]::ReadAllText('{quoted}', [System.Text.Encoding]::UTF8)"
        shell = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
        return [shell, "-NoProfile", "-Command", f"iex ({text})"]
    return ["bash", str(script)]


def step_installer(
    j: Journey, spec: str, installer_dir: Path, script: Path | None, timeout: float
) -> Step:
    """Run the installer in `installer_dir`, then make the journey use the dango it installed."""
    installer_dir.mkdir(parents=True, exist_ok=True)
    env_extra = {**INSTALLER_ANSWERS, "DANGO_INSTALL_SPEC": spec}
    proc = j.run(installer_command(script), timeout=timeout, cwd=installer_dir, env_extra=env_extra)
    if proc.error is not None:
        return failure("installer", f"installer: {proc.error}", proc.output)
    if proc.returncode != 0:
        return failure("installer", f"installer: exit code {proc.returncode}", proc.output)
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
    shown = version.output.strip().splitlines()[0] if version.output.strip() else "no version text"
    return Step("installer", "PASS", f"installed {spec} into {installer_dir / 'venv'} ({shown})")
