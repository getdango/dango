"""scripts/check_wheel_imports.py

Import every module of an installed package (default: dango) and report failures.
Run it in a fresh virtualenv after installing the built wheel. Exits 1 on any failure.

Vendored dlt connectors need per-source packages that Dango installs on demand, so a
missing-dependency error under --optional-prefix is listed as skipped, not failed.
"""

import argparse
import importlib
import pkgutil
import sys

DEFAULT_OPTIONAL = {"dango": ["dango.ingestion.dlt_sources."]}


def _is_missing_dependency(exc: BaseException) -> bool:
    return isinstance(exc, ImportError) or type(exc).__name__ == "MissingDependencyException"


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
    args = parser.parse_args()
    optional = args.optional_prefix
    if optional is None:
        optional = DEFAULT_OPTIONAL.get(args.package, [])

    root = importlib.import_module(args.package)
    failures: dict[str, str] = {}
    skipped: dict[str, str] = {}
    count = 1

    def record(name: str, exc: BaseException) -> None:
        reason = (
            f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"
        )
        if name.startswith(tuple(optional)) and _is_missing_dependency(exc):
            skipped[name] = reason
        else:
            failures[name] = reason

    def on_error(name: str) -> None:
        record(name, sys.exc_info()[1] or ImportError(name))

    for info in pkgutil.walk_packages(root.__path__, prefix=f"{args.package}.", onerror=on_error):
        count += 1
        try:
            importlib.import_module(info.name)
        except BaseException as exc:
            record(info.name, exc)

    for name, reason in sorted(skipped.items()):
        print(f"SKIP {name}: {reason}")
    for name, reason in sorted(failures.items()):
        print(f"FAIL {name}: {reason}")
    print(
        f"{args.package}: imported {count} modules, "
        f"{len(skipped)} skipped (optional), {len(failures)} failures"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
