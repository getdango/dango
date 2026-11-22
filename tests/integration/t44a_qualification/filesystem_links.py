"""tests/integration/t44a_qualification/filesystem_links.py

Bounded, read-only installed-mount closure observation; never repair or admit restore.
"""

import json
import os
import stat
from pathlib import Path

from restore_files import ROOTS, linked_target

REPORT_BOUND = 16 * 1024**2
ENTRY_BOUND = 500_000
LINK_BOUND = 25_000


def facts(path, root):
    """Record metadata/link text only; never inspect file contents or follow directory links."""
    result = {"path": str(path).replace(str(root), "$TASK")}
    try:
        st = path.lstat()
        result.update(
            mode=stat.S_IMODE(st.st_mode),
            uid=st.st_uid,
            gid=st.st_gid,
            device=st.st_dev,
            inode=st.st_ino,
            kind="link"
            if stat.S_ISLNK(st.st_mode)
            else "directory"
            if stat.S_ISDIR(st.st_mode)
            else "file"
            if stat.S_ISREG(st.st_mode)
            else "special",
        )
        if result["kind"] == "link":
            result["readlink"] = os.readlink(path)
        result["realpath"] = str(path.resolve()).replace(str(root), "$TASK")
    except FileNotFoundError:
        result["kind"] = "missing"
    return result


def node_trace(root):
    """Bind lexical mapping, every raw/destination prefix, and actual real resolution."""
    name = "work/app/project/node_modules/.bin/node"
    path = root / name
    result = {"entry": facts(path, root)}
    if result["entry"]["kind"] != "link":
        return result
    target = result["entry"]["readlink"]
    virtual = linked_target(name, target)
    result["virtual_destination"] = virtual
    mapped = root / target.lstrip("/") if target.startswith("/") else path.parent / target
    destination = root / virtual
    result.update(
        mapped_raw=str(mapped).replace(str(root), "$TASK"),
        mapped_real=str(mapped.resolve()).replace(str(root), "$TASK"),
        virtual_real=str(destination.resolve()).replace(str(root), "$TASK"),
        mapped_exists=os.path.lexists(mapped),
        destination_exists=os.path.lexists(destination),
    )
    parents = {}
    for raw in [mapped, destination]:
        for end in range(len(root.parts), len(raw.parts) + 1):
            parent = Path(*raw.parts[:end])
            parents[str(parent)] = facts(parent, root)
    result["prefixes"] = list(parents.values())
    return result


def observe(root):
    """Collect every strict lexical closure failure within the declared observation bounds."""
    assert root.resolve(strict=True) == root
    report = {
        "complete": False,
        "scope": "Installed mount closure only; no app/warehouse runtime or keys recreated",
        "native": "UNRUN",
        "n4": "UNRUN",
        "entries": 0,
        "links": [],
        "failures": [],
        "errors": [],
        "node": None,
    }
    names = set()

    def visit(path, member):
        name = path.relative_to(root).as_posix()
        if name in names:
            return
        assert len(names) < ENTRY_BOUND, "Filesystem entry observation overflow"
        names.add(name)
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            assert len(report["links"]) < LINK_BOUND, "Filesystem link observation overflow"
            row = {"entry": name, "target": os.readlink(path)}
            report["links"].append(row)
            if member == "pgdata":
                report["failures"].append(
                    {"entry": name, "reason": "Unsafe cold entry: PG symlink"}
                )
            try:
                row["virtual_destination"] = linked_target(name, row["target"])
            except AssertionError as error:
                report["failures"].append({"entry": name, "reason": str(error)})
        elif not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            report["failures"].append({"entry": name, "reason": "Unsafe cold special entry"})
        if member == "secrets" and name != "secrets/readiness" and stat.S_ISREG(st.st_mode):
            if stat.S_IMODE(st.st_mode) != 0o600:
                report["failures"].append({"entry": name, "reason": "Unsafe secret file mode"})

    try:
        for member in ROOTS:
            top = root / member
            if not top.is_dir() or top.is_symlink():
                report["failures"].append(
                    {"entry": member, "reason": "Missing or symlinked mount root"}
                )
                report["errors"].append("Untraversable installed mount root: " + member)
                continue
            visit(top, member)
            for directory, dirs, files in os.walk(
                top, followlinks=False, onerror=lambda e: report["errors"].append(str(e))
            ):
                for name in sorted(dirs + files):
                    visit(Path(directory) / name, member)
        for row in report["links"]:
            target = row.get("virtual_destination")
            if target is not None and target not in names:
                report["failures"].append(
                    {
                        "entry": row["entry"],
                        "reason": "Link target absent from backup closure",
                        "virtual_destination": target,
                    }
                )
        report["node"] = node_trace(root)
        report["complete"] = not report["errors"]
    except Exception as error:
        report["errors"].append(str(error))
    report.update(
        entries=len(names),
        link_count=len(report["links"]),
        failure_count=len(report["failures"]),
        strict_lexical_closure_pass=report["complete"] and not report["failures"],
        realpath_traversal="node trace only; lexical membership is separately reported",
    )
    return report


def write_receipt(root, report, known):
    """Check errors and data together before writing one redacted bounded public receipt."""
    text = json.dumps(report, indent=2).replace(str(root), "$TASK")
    leaked = bool(report.get("raw_secret_leak"))
    for value in known:
        assert len(value) >= 16
        if value in text:
            leaked = True
            text = text.replace(value, "[REDACTED]")
    report = json.loads(text)
    report["raw_secret_leak"] = leaked
    if leaked:
        report["complete"] = False
        report["strict_lexical_closure_pass"] = False
    report["status"] = (
        "COMPLETE_FILESYSTEM_OBSERVATION_NOT_N4"
        if report["complete"]
        else "INCOMPLETE_FILESYSTEM_OBSERVATION"
    )
    text = json.dumps(report, indent=2) + "\n"
    if len(text.encode()) > REPORT_BOUND:
        report = {
            "status": "INCOMPLETE_FILESYSTEM_OBSERVATION",
            "complete": False,
            "overflow": True,
            "raw_secret_leak": leaked,
            "observed_entries": report.get("entries"),
            "observed_failures": report.get("failure_count"),
            "n4": "UNRUN",
            "reason": "Full receipt exceeds bound; omitted detail is incomplete, never pass",
        }
        text = json.dumps(report, indent=2) + "\n"
    (root / "evidence/filesystem-probe.json").write_text(text)
    return report


def export_observation(rig):
    """Observe without changing links; ordinary owned installer cleanup still follows."""
    report = observe(rig.root)
    report["preparation"] = json.loads((rig.root / "evidence/result.json").read_text())
    observed = write_receipt(rig.root, report, rig.secrets.values())
    return {
        key: observed.get(key)
        for key in ["complete", "status", "entries", "link_count", "failure_count"]
    }
