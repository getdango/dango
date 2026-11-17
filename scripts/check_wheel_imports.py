"""scripts/check_wheel_imports.py

Import every module of an installed package (default: dango) and report failures.
Run it in a fresh virtualenv after installing the built wheel. Exits 1 on any failure.

Vendored dlt connectors need per-source packages that Dango installs on demand, so a
missing-dependency error (ModuleNotFoundError / MissingDependencyException) under --optional-prefix
is listed as skipped, not failed. Server-only modules (--server-only-prefix; they run only on Linux
servers) that fail because they name a Unix-only module or attribute are listed as expected, not
failed; any other failure in those prefixes still fails.
"""

import argparse
import importlib
import pkgutil
import re
import sys

DEFAULT_OPTIONAL = {"dango": ["dango.ingestion.dlt_sources."]}
DEFAULT_SERVER_ONLY = {"dango": ["dango.platform.cloud.", "dango.runtime."]}

_UNIX_ONLY_NAMES = (
    "fcntl|resource|pwd|grp|termios|fchmod|fchown|chown|killpg|getpgid|getpgrp|setsid"
    "|geteuid|getuid|getgid|O_NOFOLLOW|O_NONBLOCK|O_DIRECTORY|SIGHUP|SIGALRM|SIGKILL|SIGUSR1"
)
_UNIX_ONLY_RE = re.compile(rf"'(?:{_UNIX_ONLY_NAMES})'")


def _under(name: str, prefixes: list[str]) -> bool:
    return any(name == p.rstrip(".") or name.startswith(p) for p in prefixes)


def _is_missing_dependency(exc: BaseException, package: str) -> bool:
    """A genuine absent third-party package; a failing import of the project's own code is not one."""
    if type(exc).__name__ == "MissingDependencyException":
        return True
    if isinstance(exc, ModuleNotFoundError):
        return not (exc.name or "").startswith(package)
    return False


def _names_unix_only(exc: BaseException) -> bool:
    return isinstance(exc, (ImportError, AttributeError)) and bool(_UNIX_ONLY_RE.search(str(exc)))


def classify(
    name: str,
    exc: BaseException,
    optional: list[str],
    server_only: list[str],
    package: str = "dango",
) -> str:
    """Return "skip", "server_only" or "fail" for a module whose import raised ``exc``."""
    if _under(name, optional) and _is_missing_dependency(exc, package):
        return "skip"
    if _under(name, server_only) and _names_unix_only(exc):
        return "server_only"
    return "fail"


def main() -> int:
    """Import all modules under the package and print the result."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", default="dango", help="top-level package to check")
    parser.add_argument(
        "--optional-prefix",
        action="append",
        default=None,
        help="module prefix whose missing third-party imports are skipped (repeatable)",
    )
    parser.add_argument(
        "--server-only-prefix",
        action="append",
        default=None,
        help="module prefix of Linux-server-only code whose Unix-only import errors are expected",
    )
    args = parser.parse_args()
    optional = args.optional_prefix
    if optional is None:
        optional = DEFAULT_OPTIONAL.get(args.package, [])
    server_only = args.server_only_prefix
    if server_only is None:
        server_only = DEFAULT_SERVER_ONLY.get(args.package, [])

    root = importlib.import_module(args.package)
    failures: dict[str, str] = {}
    skipped: dict[str, str] = {}
    expected: dict[str, str] = {}
    count = 1

    def record(name: str, exc: BaseException) -> None:
        """Classify one import failure into skipped, expected server-only, or failed."""
        reason = (
            f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"
        )
        kind = classify(name, exc, optional, server_only, args.package)
        if kind == "skip":
            skipped[name] = reason
        elif kind == "server_only":
            expected[name] = reason
        else:
            failures[name] = reason

    def on_error(name: str) -> None:
        """Record a package that pkgutil could not import while walking."""
        record(name, sys.exc_info()[1] or ImportError(name))

    for info in pkgutil.walk_packages(root.__path__, prefix=f"{args.package}.", onerror=on_error):
        count += 1
        try:
            importlib.import_module(info.name)
        except BaseException as exc:
            record(info.name, exc)

    for name, reason in sorted(skipped.items()):
        print(f"SKIP {name}: {reason}")
    for name, reason in sorted(expected.items()):
        print(f"EXPECTED (server-only, Unix-only import) {name}: {reason}")
    for name, reason in sorted(failures.items()):
        print(f"FAIL {name}: {reason}")
    print(
        f"{args.package}: imported {count} modules, "
        f"{len(skipped)} skipped (optional), {len(expected)} expected server-only, "
        f"{len(failures)} failures"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
