"""scripts/first_user_journey.py

Walk a first-user Dango session in a fresh work directory and print PASS/FAIL/SKIP per step:
init, add-source, sync, count, dbt, web (start, stop, restart), second-sync, and, only with
--with-docker, docker (start, status, stop). With --installer the journey first runs the
checkout's install.sh / install.ps1 with the built wheel and continues with the dango it
installed. Used by the platform-smoke workflow and by a person on a real machine
(scripts/first_user_journey.ps1). Standard library only.
"""

import argparse
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from journey_installer import DEFAULT_INSTALLER_TIMEOUT_SECONDS, step_installer
from journey_services import docker_step, web_step
from journey_support import (
    DEFAULT_BUDGET_SECONDS,
    Journey,
    Proc,
    Step,
    failure,
    flagged_rows,
    free_ports,
    report,
    safe,
)

DEFAULT_STEP_TIMEOUT_SECONDS = 300
DEFAULT_STOP_GRACE_SECONDS = 15

PEOPLE_CSV = "id,name,city\n1,Ada,London\n2,Bao,Hanoi\n3,Zoë,Zürich\n4,Dmitri,Perth\n5,Eve,Lima\n"
SOURCES_YML = (
    "version: '1.0'\nsources:\n  - name: people\n    type: csv\n    csv:\n"
    "      directory: data/uploads\n"
)
EXPECTED_ROWS = 5

# Step -> the steps it needs. A step whose dependency did not PASS is reported as SKIP.
DEPENDS: dict[str, list[str]] = {
    "add-source": ["init"],
    "sync": ["add-source"],
    "count": ["sync"],
    "dbt": ["sync"],
    "web": ["init"],
    "second-sync": ["count"],
    "docker": ["init"],
}

_SET_PORTS = """
import sys, yaml
path = sys.argv[1]
ports = dict(zip(("port", "metabase_port", "dbt_docs_port", "marimo_port"), map(int, sys.argv[2:])))
with open(path, encoding="utf-8") as fh:
    data = yaml.safe_load(fh) or {}
data.setdefault("platform", {}).update(ports)
with open(path, "w", encoding="utf-8", newline="\\n") as fh:
    yaml.safe_dump(data, fh, sort_keys=False)
print(" ".join("%s=%s" % kv for kv in ports.items()))
"""

_COUNT_ROWS = """
import sys, duckdb
con = duckdb.connect(sys.argv[1], read_only=True)
rows = con.execute(
    "select table_schema, table_name from information_schema.tables "
    "where table_type = 'BASE TABLE' and table_name ilike '%people%' "
    "and table_schema not in ('information_schema', 'pg_catalog') order by 1, 2"
).fetchall()
if not rows:
    print("NO_TABLE")
    sys.exit(3)
rows.sort(key=lambda r: (not r[0].startswith("raw"), r[0], r[1]))
schema, table = rows[0]
count = con.execute('select count(*) from "%s"."%s"' % (schema, table)).fetchone()[0]
named = con.execute(
    'select count(*) from "%s"."%s" where name = ?' % (schema, table), ["Zo\\u00eb"]
).fetchone()[0]
print("COUNT %s.%s %d" % (schema, table, count))
print("NAMED %d" % named)
"""


def _fail_from(name: str, proc: Proc, what: str) -> Step | None:
    """FAIL step for a command that did not exit 0, else None."""
    if proc.error is not None:
        return failure(name, f"{what}: {proc.error}", proc.output)
    if proc.returncode != 0:
        return failure(name, f"{what}: exit code {proc.returncode}", proc.output)
    return None


def step_init(j: Journey) -> Step:
    """Run dango init --skip-wizard, check the project files, set non-default ports."""
    j.workdir.mkdir(parents=True, exist_ok=True)
    proc = j.dango_run("init", "--skip-wizard")
    bad = _fail_from("init", proc, "dango init --skip-wizard")
    if bad:
        return bad
    if "Traceback (most recent call last)" in proc.output:
        return failure("init", "traceback in output", proc.output)
    if "Completed with Warnings" in proc.output:
        return failure("init", "init completed with warnings", proc.output)
    for name in ("project.yml", "auth.yml"):
        if not (j.workdir / ".dango" / name).is_file():
            return failure("init", f".dango/{name} was not created", proc.output)
    ports = [str(p) for p in (j.fixed_ports[:4] or free_ports(4))]
    set_ports = j.run(
        [j.python, "-c", _SET_PORTS, str(j.workdir / ".dango" / "project.yml"), *ports]
    )
    bad = _fail_from("init", set_ports, "setting non-default ports")
    if bad:
        return bad
    return Step("init", "PASS", f"project created; ports {set_ports.output.strip()}")


def step_add_source(j: Journey) -> Step:
    """Write a CSV and sources.yml, then check source list and validate."""
    uploads = j.workdir / "data" / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    with open(uploads / "people.csv", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(PEOPLE_CSV)
    with open(j.workdir / ".dango" / "sources.yml", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SOURCES_YML)
    listed = j.dango_run("source", "list")
    bad = _fail_from("add-source", listed, "dango source list")
    if bad:
        return bad
    if "people" not in listed.output:
        return failure("add-source", "source list does not show 'people'", listed.output)
    valid = j.dango_run("validate")
    bad = _fail_from("add-source", valid, "dango validate")
    if bad:
        rows = flagged_rows(valid.output)
        failed = [text for status, text in rows if status == "FAIL"]
        if failed:
            bad.detail += " (failed checks: " + "; ".join(failed) + ")"
        bad.flagged = "\n".join(f"{status} {text}" for status, text in rows)
        return bad
    return Step("add-source", "PASS", "csv source 'people' written, listed and validated")


def step_sync(j: Journey) -> Step:
    """Run dango sync --yes."""
    proc = j.dango_run("sync", "--yes")
    bad = _fail_from("sync", proc, "dango sync --yes")
    return bad or Step("sync", "PASS")


def _count(j: Journey, name: str) -> tuple[int | None, int, Step | None, str]:
    """Row count of the loaded people table and how many rows hold the non-ASCII name.

    Returns (count, rows named Zo\u00eb, failure step, 'schema.table').
    """
    warehouse = j.workdir / "data" / "warehouse.duckdb"
    proc = j.run([j.python, "-c", _COUNT_ROWS, str(warehouse)])
    bad = _fail_from(name, proc, "reading the warehouse")
    if bad:
        reason = (
            "no table containing 'people' in the warehouse"
            if "NO_TABLE" in proc.output
            else bad.detail
        )
        return None, 0, failure(name, reason, proc.output), ""
    line = next((ln for ln in proc.output.splitlines() if ln.startswith("COUNT ")), "")
    parts = line.split()
    named = next((ln.split() for ln in proc.output.splitlines() if ln.startswith("NAMED ")), [])
    if len(parts) != 3 or len(named) != 2:
        return None, 0, failure(name, "unexpected count output", proc.output), ""
    return int(parts[2]), int(named[1]), None, parts[1]


def step_count(j: Journey) -> Step:
    """Check the loaded people table has the expected number of rows."""
    count, named, bad, table = _count(j, "count")
    if bad:
        return bad
    if count != EXPECTED_ROWS:
        return failure("count", f"{table} has {count} rows, expected {EXPECTED_ROWS}")
    if named != 1:
        return failure(
            "count",
            f"{table}: the non-ASCII value 'Zo\u00eb' is in {named} rows, expected 1 "
            "(mis-decoded on load?)",
        )
    j.counted = (table, count)
    return Step("count", "PASS", f"{table} has {count} rows, non-ASCII value intact")


def step_dbt(j: Journey) -> Step:
    """Run dango run (dbt build) and report whether the project has models."""
    proc = j.dango_run("run")
    bad = _fail_from("dbt", proc, "dango run")
    if bad:
        return bad
    models = (
        list((j.workdir / "dbt" / "models").rglob("*.sql")) if (j.workdir / "dbt").is_dir() else []
    )
    note = f"{len(models)} model file(s)" if models else "no dbt models in the project"
    return Step("dbt", "PASS", f"dango run exited 0 ({note})")


def step_second_sync(j: Journey) -> Step:
    """Sync again; the same table must still have the same row count (csv is replace mode)."""
    first_table, before = j.counted or ("", EXPECTED_ROWS)
    proc = j.dango_run("sync", "--yes")
    bad = _fail_from("second-sync", proc, "dango sync --yes")
    if bad:
        return bad
    count, _, bad, table = _count(j, "second-sync")
    if bad:
        return bad
    if first_table and table != first_table:
        return failure(
            "second-sync", f"counted {table} after the repeat sync, but {first_table} before"
        )
    if count != before:
        return failure(
            "second-sync", f"row count changed from {before} to {count} on a repeat sync"
        )
    return Step("second-sync", "PASS", f"{table} still has {count} rows")


def _absolute_if_path(value: str) -> str:
    """Absolute form of a path-like value; a bare command name is resolved on PATH."""
    if os.sep in value or "/" in value:
        return os.path.abspath(value)
    return shutil.which(value) or value


def _absolute_spec(value: str) -> str:
    """Absolute path when `value` names an existing file, else the spec unchanged."""
    return os.path.abspath(value) if os.path.exists(value) else value


def _checked_paths(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Make the path arguments absolute and reject conflicting or missing options."""
    if args.installer:
        if not args.spec:
            parser.error("--installer requires --spec (a built wheel path or a pip spec)")
        if args.dango or args.python:
            parser.error("--installer runs the dango the installer installs; drop --dango/--python")
        args.spec = _absolute_spec(args.spec)
    else:
        if not args.dango:
            parser.error("--dango is required unless --installer is given")
        if args.spec:
            parser.error("--spec is only used with --installer")
    # Children run with cwd=workdir, so every path must be absolute before they start.
    args.workdir = args.workdir.absolute()
    args.log = args.log.absolute()
    args.dango = _absolute_if_path(args.dango) if args.dango else None
    args.python = _absolute_if_path(args.python) if args.python else None
    args.installer_script = args.installer_script.absolute() if args.installer_script else None
    args.installer_dir = args.workdir.with_name(args.workdir.name + "-installer")
    folders = [("--workdir", args.workdir)]
    if args.installer:
        folders.append(("the installer directory", args.installer_dir))
    for label, folder in folders:
        if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
            parser.error(safe(f"{label} {folder} is not empty; use a new or empty directory"))


def main() -> int:
    """Parse arguments, run the steps in order and report; returns the exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dango", help="path to the dango executable (not with --installer)")
    parser.add_argument(
        "--workdir", required=True, type=Path, help="new or empty directory for the project"
    )
    parser.add_argument("--log", required=True, type=Path, help="log file (appended, UTF-8)")
    parser.add_argument("--with-docker", action="store_true", help="also run the docker step")
    parser.add_argument(
        "--installer",
        action="store_true",
        help="first run install.sh / install.ps1 non-interactively (needs --spec), then use its dango",
    )
    parser.add_argument("--spec", help="with --installer: built wheel path or pip spec to install")
    parser.add_argument(
        "--installer-timeout", type=float, default=DEFAULT_INSTALLER_TIMEOUT_SECONDS
    )
    parser.add_argument("--installer-script", type=Path, help=argparse.SUPPRESS)  # tests
    parser.add_argument("--step-timeout", type=float, default=DEFAULT_STEP_TIMEOUT_SECONDS)
    parser.add_argument(
        "--isolate-keyring", action="store_true", help="use the fail keyring backend"
    )
    parser.add_argument("--python", help="python of the dango venv (default: next to --dango)")
    parser.add_argument("--stop-grace", type=float, default=DEFAULT_STOP_GRACE_SECONDS)
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET_SECONDS,
        help="total seconds for all steps; step timeouts shrink to what is left",
    )
    parser.add_argument(
        "--utf8-mode",
        action="store_true",
        help="run the dango child processes in Python UTF-8 mode (PYTHONUTF8=1)",
    )
    parser.add_argument(
        "--legacy-stdio",
        action="store_true",
        help="do not set PYTHONIOENCODING=utf-8 for the dango child processes",
    )
    parser.add_argument("--steps", help=argparse.SUPPRESS)  # comma separated subset (tests)
    parser.add_argument("--output-encoding", help=argparse.SUPPRESS)  # override (tests)
    parser.add_argument(  # five comma separated ports: project (4) and web (1), for parallel tests
        "--ports", type=lambda v: tuple(int(p) for p in v.split(",")), help=argparse.SUPPRESS
    )
    parser.add_argument("--port-free-timeout", type=float, default=10, help=argparse.SUPPRESS)
    parser.add_argument("--up-timeout", type=float, default=60, help=argparse.SUPPRESS)
    args = parser.parse_args()
    _checked_paths(parser, args)

    # With --installer the installer step sets both, from the venv it creates.
    dango = Path(args.dango) if args.dango else None
    python = args.python or (
        str(dango.parent / ("python.exe" if sys.platform == "win32" else "python")) if dango else ""
    )
    j = Journey(
        args.dango or "",
        python,
        args.workdir,
        args.log,
        args.step_timeout,
        args.isolate_keyring,
        budget=args.budget,
        utf8_mode=args.utf8_mode,
        legacy_stdio=args.legacy_stdio,
        port_free_timeout=args.port_free_timeout,
        up_timeout=args.up_timeout,
        output_encoding_override=args.output_encoding,
        fixed_ports=args.ports or (),
    )
    args.log.parent.mkdir(parents=True, exist_ok=True)
    j.note(
        f"# first-user journey: dango={dango or '(from the installer)'} python={python or '(from the installer)'}"
        f" workdir={args.workdir}"
    )

    depends = {**DEPENDS, "init": ["installer"]} if args.installer else DEPENDS
    steps: list[tuple[str, Callable[[Journey], Step]]] = [
        ("init", step_init),
        ("add-source", step_add_source),
        ("sync", step_sync),
        ("count", step_count),
        ("dbt", step_dbt),
        ("web", lambda jj: web_step(jj, args.stop_grace)),
        ("second-sync", step_second_sync),
    ]
    if args.installer:
        steps.insert(
            0,
            (
                "installer",
                lambda jj: step_installer(
                    jj, args.spec, args.installer_dir, args.installer_script, args.installer_timeout
                ),
            ),
        )
    if args.with_docker:
        steps.append(("docker", docker_step))
    selected = set(args.steps.split(",")) if args.steps else None
    if selected is not None:
        steps = [(n, f) for n, f in steps if n in selected]
    failed = False
    for name, fn in steps:
        missing = next(
            (
                d
                for d in depends.get(name, [])
                if d not in j.results or j.results[d].status != "PASS"
            ),
            None,
        )
        if missing:
            step = Step(name, "SKIP", f"depends on {missing}")
        else:
            try:
                step = fn(j)
            except Exception as exc:  # a crashing step must not stop the run
                step = failure(name, f"{type(exc).__name__}: {exc}")
        j.results[name] = step
        failed = failed or step.status == "FAIL"
        report(step)
    if not args.with_docker and (selected is None or "docker" in selected):
        report(Step("docker", "SKIP", "not requested"))
    print(safe(f"log: {args.log}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
