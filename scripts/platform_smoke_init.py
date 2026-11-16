"""scripts/platform_smoke_init.py

Run `dango init --skip-wizard` in a work directory and check it produced a project.
Prints one PASS/FAIL line for the label; used by the platform smoke workflow.
"""

import argparse
import subprocess
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 300
FAIL_TAIL_LINES = 40
PASS_TAIL_LINES = 15


def _text(data: str | bytes | None) -> str:
    """Return captured output as text; TimeoutExpired carries bytes on POSIX."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data


def _safe(text: str) -> str:
    """Escape non-ASCII so printing cannot fail under a cp1252 stdout."""
    return text.encode("ascii", errors="backslashreplace").decode("ascii")


def _check(dango: str, workdir: Path, timeout: float) -> tuple[str | None, str]:
    """Run init; return (failure reason or None, combined output)."""
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [dango, "init", "--skip-wizard"],
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return f"timed out after {timeout:g}s", _text(exc.stdout) + _text(exc.stderr)
    except OSError as exc:
        return f"could not run {dango}: {exc}", ""

    output = _text(proc.stdout) + _text(proc.stderr)
    if proc.returncode != 0:
        return f"exit code {proc.returncode}", output
    if "Traceback (most recent call last)" in output:
        return "traceback in output", output
    if "Completed with Warnings" in output:
        return "init completed with warnings", output
    for name in ("project.yml", "auth.yml"):
        if not (workdir / ".dango" / name).is_file():
            return f".dango/{name} was not created", output
    return None, output


def main() -> int:
    """Run the init check and report; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dango", required=True, help="path to the dango executable")
    parser.add_argument("--workdir", required=True, type=Path, help="empty directory to init in")
    parser.add_argument("--label", required=True, help="name printed in the PASS/FAIL line")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()

    reason, output = _check(args.dango, args.workdir, args.timeout)
    lines = output.splitlines()
    label = _safe(args.label)
    if reason is None:
        print(f"PASS {label}")
        print("--- last output lines ---")
        print(_safe("\n".join(lines[-PASS_TAIL_LINES:])))
        return 0
    print(f"FAIL {label}: {_safe(reason)}")
    print("--- last output lines ---")
    print(_safe("\n".join(lines[-FAIL_TAIL_LINES:])))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
