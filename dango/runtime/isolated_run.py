"""dango/runtime/isolated_run.py

Entry point for user code that runs as a restricted OS user (``dango-run`` for
scripts, ``dango-nb`` for the Marimo process).  Standard library only: it must
not import anything from ``dango`` (those modules read ``.dango/``).

Started as ``python -I -m dango.runtime.isolated_run --role ROLE --cwd PROJECT
[--check-only] [-- TARGET...]``.  It

1. sets ``umask 077`` and disables core dumps;
2. checks, as the runner uid, that every protected path is unreadable and exits
   70 ("fail closed") if one is readable;
3. with ``--check-only`` stops there (exit 0);
4. otherwise changes into the project and ``exec``s the target: for the script
   role ``python SCRIPT ARGS...`` (SCRIPT must be a regular file under
   ``scripts/``), for the nb role ``python -m marimo ARGS...``.
"""

from __future__ import annotations

import argparse
import os
import resource
import sys

EXIT_USAGE = 64
EXIT_BROKEN = 70

ROLES = ("script", "nb")
SRV_DIR = "/srv/dango"
#: Absolute paths neither runner may read (the project-relative ones are added below).
ABSOLUTE_DENIED = (
    f"{SRV_DIR}/backups",
    f"{SRV_DIR}/secrets",
    f"{SRV_DIR}/metabase",
    "/home/dango",
)
#: Project-relative paths neither runner may read.
PROJECT_DENIED = (".env", ".dango", ".dlt")
#: Project-relative paths only the notebook runner may not read (scripts may).
NB_ONLY_DENIED = ("data",)


def denied_paths(role: str, project: str) -> list[str]:
    """Every path the *role* must not be able to read, for *project*."""
    names = PROJECT_DENIED + (NB_ONLY_DENIED if role == "nb" else ())
    return [os.path.join(project, name) for name in names] + list(ABSOLUTE_DENIED)


def readable(path: str) -> bool:
    """True when this process could read *path* (list or traverse, for directories)."""
    if os.path.isdir(path):
        return os.access(path, os.R_OK) or os.access(path, os.X_OK)
    return os.access(path, os.R_OK)


def preflight(role: str, project: str) -> str | None:
    """Return the first readable denied path, or ``None`` when isolation holds."""
    for path in denied_paths(role, project):
        if readable(path):
            return path
    return None


def _script_target(project: str, relative: str) -> str | None:
    """Resolved script path if it is a regular file inside ``<project>/scripts``."""
    base = os.path.realpath(os.path.join(project, "scripts"))
    path = os.path.realpath(os.path.join(project, relative))
    if os.path.commonpath([base, path]) != base or path == base:
        return None
    return path if os.path.isfile(path) else None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="isolated_run", add_help=False)
    parser.add_argument("--role", required=True, choices=ROLES)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("target", nargs="*")
    return parser


def _usage(message: str) -> int:
    sys.stderr.write(f"isolated_run: {message}\n")
    return EXIT_USAGE


def main(argv: list[str] | None = None) -> int:
    """Run the preflight, then exec the target; returns an exit code on refusal."""
    args = list(sys.argv[1:] if argv is None else argv)
    target: list[str] = []
    head, sep = args, False
    if "--" in args:
        split = args.index("--")
        head, sep, target = args[:split], True, args[split + 1 :]
    try:
        parsed = _build_parser().parse_args(head)
    except SystemExit:
        return EXIT_USAGE
    if parsed.target:
        return _usage("unexpected arguments before --")
    project = os.path.realpath(parsed.cwd)
    if not os.path.isdir(project):
        return _usage(f"not a directory: {parsed.cwd}")
    if not parsed.check_only and not (sep and target):
        return _usage("no target given")

    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    exposed = preflight(parsed.role, project)
    if exposed is not None:
        sys.stderr.write(f"runner isolation is broken: {exposed} is readable\n")
        return EXIT_BROKEN
    if parsed.check_only:
        return 0

    os.chdir(project)
    if parsed.role == "script":
        script = _script_target(project, target[0])
        if script is None:
            return _usage(f"not a script under scripts/: {target[0]}")
        command = [sys.executable, script, *target[1:]]
    else:
        command = [sys.executable, "-m", "marimo", *target]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, command)
    return EXIT_USAGE  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main())
