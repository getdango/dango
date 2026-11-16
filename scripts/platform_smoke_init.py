"""scripts/platform_smoke_init.py

Run `dango init --skip-wizard` in a work directory and check it produced a project.
Prints one PASS/FAIL line for the label; used by the platform smoke workflow.
"""

import argparse
import subprocess
from pathlib import Path

TIMEOUT_SECONDS = 300
TAIL_LINES = 40


def _check(dango: str, workdir: Path) -> tuple[str | None, str]:
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
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        return f"timed out after {TIMEOUT_SECONDS}s", out
    except OSError as exc:
        return f"could not run {dango}: {exc}", ""

    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return f"exit code {proc.returncode}", output
    if "Traceback (most recent call last)" in output:
        return "traceback in output", output
    if not (workdir / ".dango" / "project.yml").is_file():
        return ".dango/project.yml was not created", output
    return None, output


def main() -> int:
    """Run the init check and report; returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dango", required=True, help="path to the dango executable")
    parser.add_argument("--workdir", required=True, type=Path, help="empty directory to init in")
    parser.add_argument("--label", required=True, help="name printed in the PASS/FAIL line")
    args = parser.parse_args()

    reason, output = _check(args.dango, args.workdir)
    if reason is None:
        print(f"PASS {args.label}")
        return 0
    tail = "\n".join(output.splitlines()[-TAIL_LINES:])
    # Escape non-ASCII so printing cannot fail under a cp1252 stdout.
    safe_tail = tail.encode("ascii", errors="backslashreplace").decode("ascii")
    print(f"FAIL {args.label}: {reason}")
    print("--- last output lines ---")
    print(safe_tail)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
