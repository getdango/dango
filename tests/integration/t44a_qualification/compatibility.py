"""tests/integration/t44a_qualification/compatibility.py

N3 physical-file controls and shared bounded native identity checks.
"""

import hashlib
import importlib.metadata
import importlib.util
import json
import shutil
import sys
import zipfile
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "compatibility-pins.json").read_text())
ROOT = Path("/work/n3")
ANSWER = [[3, 350, 3]]
QUERY = "SELECT count(*),sum(amount),count(DISTINCT id) FROM orders"


def digest(data):
    """Bind original artifact, metadata and physical bytes."""
    return hashlib.sha256(data).hexdigest()


def safe(data):
    """Refuse raw synthetic credentials before saving any native diagnostic."""
    known = json.loads(Path("/secrets/catalog.json").read_text()).values()
    assert not any(value.encode() in data for value in known), "N3 raw secret exposure"
    return data


def save(stage, report):
    """Bound all original N3 reports together, separately from their export copies."""
    data = safe(json.dumps(report, indent=2, default=str).encode() + b"\n")
    target = Path("/work", "n3-" + stage + ".json")
    total = sum(p.stat().st_size for p in Path("/work").glob("n3-*.json") if p != target)
    assert total + len(data) <= PINS["report_bound"], "N3 aggregate report bound"
    target.write_bytes(data)


def identity(version):
    """Check exact installed wheel/native/notices before importing either engine."""
    pin = (
        PINS["old"]
        if version == "1.5.2"
        else next(
            p
            for p in json.loads((HERE / "pins.json").read_text())["artifacts"]
            if p.get("name") == "duckdb"
        )
    )
    archive = Path("/tools/artifacts", pin["file"])
    assert digest(archive.read_bytes()) == pin["sha256"]
    dist = importlib.metadata.distribution("duckdb")
    assert dist.version == version
    with zipfile.ZipFile(archive) as wheel:
        names = [n for n in wheel.namelist() if n.startswith("_duckdb.") and n.endswith(".so")]
        assert len(names) == 1
        native_hash = digest(wheel.read(names[0]))
        for name in wheel.namelist():
            if not name.endswith("/") and ("LICENSE" in name or "NOTICE" in name):
                assert digest(dist.locate_file(name).read_bytes()) == digest(wheel.read(name))
    spec = importlib.util.find_spec("_duckdb")
    assert spec and Path(spec.origin).resolve().is_relative_to(Path(sys.prefix).resolve())
    assert digest(Path(spec.origin).read_bytes()) == native_hash
    import duckdb

    assert duckdb.__version__ == version
    return {
        "version": version,
        "wheel_sha256": pin["sha256"],
        "native_sha256": native_hash,
        "native_path": spec.origin,
        "prefix": sys.prefix,
    }


def connect(stage, file=":memory:", readonly=False):
    """Use a task-owned zero-spill single-thread native session."""
    import duckdb

    directory = Path("/work/n3-spill") / stage
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert directory.resolve() == directory and not directory.is_symlink()
    con = duckdb.connect(
        str(file),
        read_only=readonly,
        config={
            "memory_limit": "128MB",
            "threads": "1",
            "temp_directory": str(directory),
            "max_temp_directory_size": "0B",
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "allow_unsigned_extensions": "false",
        },
    )
    try:
        assert con.execute(
            "SELECT current_setting('threads'),current_setting('temp_directory'),"
            "current_setting('max_temp_directory_size'),current_setting('allow_unsigned_extensions'),"
            "current_setting('autoinstall_known_extensions'),current_setting('autoload_known_extensions')"
        ).fetchone() == (1, str(directory), "0 bytes", False, False, False)
        return con
    except Exception:
        con.close()
        raise


def execute(con, sql, report):
    """Retain actual native SQL and diagnostics without interpolated credentials."""
    row = {"sql": sql}
    report.setdefault("sql", []).append(row)
    try:
        result = con.execute(sql)
        row["status"] = "executed"
        return result
    except Exception as error:
        safe(str(error).encode())
        row.update(status="native_error", type=type(error).__name__, message=str(error))
        raise


def refused(error, kind, clauses, target):
    """Only the declared native exception and exact target can count as refusal."""
    safe(str(error).encode())
    assert isinstance(error, kind), "Wrong N3 native refusal class"
    assert target in str(error) and all(part in str(error) for part in clauses), (
        "Wrong N3 native refusal diagnostic/target"
    )
    return {"type": type(error).__name__, "message": str(error)}


def fingerprint(file):
    """Bind closed physical files and reject WAL, links or unbounded growth."""
    assert file.parent == ROOT and file.is_file() and not file.is_symlink()
    assert not Path(str(file) + ".wal").exists()
    stat = file.stat()
    assert stat.st_size <= PINS["physical_file_bound"]
    assert stat.st_blocks * 512 <= PINS["physical_file_bound"]
    data = file.read_bytes()
    return {
        "sha256": digest(data),
        "bytes": len(data),
        "allocated": stat.st_blocks * 512,
        "header4096_sha256": digest(data[:4096]),
        "header256_hex": data[:256].hex(),
    }


def physical(stage, report):
    """Separate legacy controls, explicit new storage, mismatch refusal and recovery."""
    import duckdb

    assert stage in {
        "create-old",
        "read-old",
        "read-existing",
        "create-new",
        "refuse-old",
        "recover-new",
        "read-old-recovery",
    }
    ROOT.mkdir(mode=0o700, exist_ok=True)
    assert ROOT.resolve() == ROOT
    file = ROOT / (
        "legacy.db"
        if stage in {"create-old", "read-old", "read-existing", "read-old-recovery"}
        else "current.db"
    )
    if stage in {"create-old", "create-new"}:
        assert not file.exists()
        with closing(connect(stage, file if stage == "create-old" else ":memory:")) as con:
            prefix = ""
            if stage == "create-new":
                execute(con, f"ATTACH '{file}' AS physical (STORAGE_VERSION 'v2.0.0')", report)
                prefix = "physical."
            execute(con, f"CREATE TABLE {prefix}orders(id INTEGER,amount INTEGER)", report)
            execute(con, f"INSERT INTO {prefix}orders VALUES (1,100),(2,200),(3,50)", report)
            execute(con, "CHECKPOINT" + (" physical" if prefix else ""), report)
            if prefix:
                execute(con, "DETACH physical", report)
        backup = file.with_suffix(".backup")
        assert not backup.exists()
        shutil.copyfile(file, backup)
        backup.chmod(0o444)
    before = fingerprint(file)
    backup = fingerprint(file.with_suffix(".backup"))
    assert before["sha256"] == backup["sha256"] and before["bytes"] == backup["bytes"], (
        "Physical backup differs before N3 read"
    )
    if stage == "refuse-old":
        assert file == ROOT / "current.db"
        report["attempt"] = {
            "api": "duckdb.connect",
            "database": str(file),
            "read_only": True,
            "file_identity": before,
        }
        try:
            with closing(connect(stage, file, True)):
                raise AssertionError("Old client unexpectedly accepted new storage")
        except duckdb.IOException as error:
            report["refusal"] = refused(
                error, duckdb.IOException, PINS["physical_error"], PINS["physical_error"][0]
            )
    else:
        with closing(connect(stage, file, True)) as con:
            report["actual"] = execute(con, QUERY, report).fetchall()
            assert report["actual"] == [tuple(ANSWER[0])]
            report["engine"] = execute(con, "SELECT version()", report).fetchone()[0]
    report.update(
        before=before, after=fingerprint(file), backup=fingerprint(file.with_suffix(".backup"))
    )
    assert report["before"] == report["after"]
    assert report["after"]["sha256"] == report["backup"]["sha256"]


def main():
    """Keep old and current engines in distinct fresh processes, never one interpreter."""
    stage, version = sys.argv[1:]
    old_stages = {"create-old", "read-old", "refuse-old", "read-old-recovery"}
    assert version == ("1.5.2" if stage in old_stages else "2.0.0")
    report = {"status": "FAIL", "stage": stage}
    try:
        report["identity"] = identity(version)
        if stage.startswith("catalog-"):
            assert version == "2.0.0"
            from compatibility_catalog import run

            run(stage, report)
        else:
            physical(stage, report)
        report["status"] = "PASS"
    except Exception as error:
        safe(str(error).encode())
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        save(stage, report)


if __name__ == "__main__":
    main()
