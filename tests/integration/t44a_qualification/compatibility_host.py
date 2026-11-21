"""tests/integration/t44a_qualification/compatibility_host.py

Serial N3 recipe using the existing native launcher, capture and owned cleanup.
"""

import json

from compatibility import PINS, digest
from held_reader import pg_snapshot

EXTRA_PEAK = PINS["extra_peak"]
STATUS = "PASS_N3_PHYSICAL_AND_CATALOG_COMPATIBILITY"
PATHS = ["fixture/n3-spec10", "work/n3", "work/n3-old", "work/n3-install-temp", "work/n3-spill"]
FILES = [
    "compatibility.py",
    "compatibility_catalog.py",
    "compatibility_host.py",
    "compatibility-pins.json",
    "requirements-compatibility-old.txt",
]


def install_command():
    """Reuse frozen pip's supported target-interpreter option, entirely offline."""
    return (
        "mkdir /work/n3-install-temp && /tools/python/bin/python3.14 -m venv --without-pip /work/n3-old && "
        "TMPDIR=/work/n3-install-temp /work/venv/bin/python -m pip --python /work/n3-old install "
        "--no-index --find-links=/tools/artifacts --require-hashes --no-deps --no-compile "
        "--no-cache-dir -r /scripts/requirements-compatibility-old.txt"
    )


def psql(rig, statement):
    """Use existing dedicated PG credentials and retain only the owned SQL command."""
    return rig.call(
        [
            "docker",
            "exec",
            rig.pg,
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "probe",
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-c",
            statement,
        ]
    ).stdout.strip()


def allocated(rig):
    """Measure all new owned trees, reports and native PG relations against the reserve."""
    paths = [
        rig.root / name
        for name in [
            "work/n3",
            "work/n3-old",
            "work/n3-install-temp",
            "work/n3-spill",
            "fixture/n3-spec10",
        ]
    ]
    paths += list((rig.root / "work").glob("n3-*.json"))
    sizes = {
        p: int(rig.call(["sudo", "du", "-s", "--block-size=1", str(p)]).stdout.split()[0])
        for p in paths
        if p.exists()
    }
    amount = sum(sizes.values())
    relations = psql(
        rig,
        "SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)::bigint "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='n3_v10'",
    )
    amount += int(relations)
    assert int(relations) + sizes.get(rig.root / "fixture/n3-spec10", 0) <= PINS["clone_bound"], (
        "N3 PG catalog plus data clone bound"
    )
    assert amount <= EXTRA_PEAK, "N3 actual owned allocations exceeded reserve"
    return amount


def cleanup(rig, report):
    """Remove only the proven newly created schema and exact fresh N3 paths."""
    if report.get("owned_paths") != PATHS:
        report["native_scratch_cleanup"] = "NO_NEW_OWNERSHIP"
        return
    found = psql(rig, "SELECT oid::bigint FROM pg_namespace WHERE nspname='n3_v10'")
    ownership = rig.root / "work/n3-catalog-create10.json"
    if found:
        assert report.get("schema_absent_before") is True, "Schema ownership not established"
        assert ownership.is_file(), "Missing native namespace ownership receipt; retained"
        owner = json.loads(ownership.read_text())["owned_schema"]
        assert owner == {"name": "n3_v10", "oid": int(found), "absent_before": True}
        psql(rig, "DROP SCHEMA n3_v10 CASCADE")
        assert not psql(rig, "SELECT oid::bigint FROM pg_namespace WHERE nspname='n3_v10'")
    for relative in PATHS:
        path = rig.root / relative
        assert path.resolve() == path and not path.is_symlink()
        if path.exists():
            rig.call(["sudo", "rm", "-rf", "--", str(path)])
        assert not path.exists()
    report["native_scratch_cleanup"] = "PASS"


def run(rig, disk_gate):
    """Reach P1–P5 and C1–C5 in fresh serial children with actual dump invariance."""
    report = {
        "status": "FAIL",
        "steps": [],
        "pins_sha256": digest((rig.root / "scripts/compatibility-pins.json").read_bytes()),
    }
    before = None
    try:
        for relative in PATHS:
            assert not (rig.root / relative).exists(), "N3 requires fresh owned paths"
        report["owned_paths"] = PATHS
        assert not psql(rig, "SELECT oid FROM pg_namespace WHERE nspname='n3_v10'")
        report["schema_absent_before"] = True
        disk_gate(rig, EXTRA_PEAK, "before-n3-old-client-offline-install")
        rig.run("producer", ["-c", install_command()])
        before = pg_snapshot(rig)
        report["catalog_before"] = before
        baseline = before
        stages = [
            ("create-old", "1.5.2"),
            ("read-existing", "2.0.0"),
            ("read-old", "1.5.2"),
            ("create-new", "2.0.0"),
            ("refuse-old", "1.5.2"),
            ("recover-new", "2.0.0"),
            ("read-old-recovery", "1.5.2"),
            ("catalog-control11", "2.0.0"),
            ("catalog-down", "2.0.0"),
            ("catalog-recover11", "2.0.0"),
            ("catalog-create10", "2.0.0"),
            ("catalog-up", "2.0.0"),
            ("catalog-default10", "2.0.0"),
            ("catalog-explicit10", "2.0.0"),
            ("catalog-default11", "2.0.0"),
        ]
        for stage, version in stages:
            disk_gate(rig, EXTRA_PEAK, "before-n3-" + stage)
            executable = (
                "/work/n3-old/bin/python" if version == "1.5.2" else "/work/venv/bin/python"
            )
            rig.run(
                "producer",
                ["-c", executable + " /scripts/compatibility.py " + stage + " " + version],
                timeout=900,
            )
            path = rig.root / "work" / ("n3-" + stage + ".json")
            data = path.read_bytes()
            native = json.loads(data)
            assert native["status"] == "PASS" and native["identity"]["version"] == version, (
                "N3 child receipt/status/client mismatch: " + stage
            )
            actual = pg_snapshot(rig)
            if stage == "catalog-create10":
                assert actual != baseline, "Native spec1.0 creation did not change catalog"
                baseline = actual
            else:
                assert actual == baseline, "N3 read/mismatch changed catalog or original Parquet"
            report["steps"].append(
                {
                    "stage": stage,
                    "version": version,
                    "receipt_sha256": digest(data),
                    "catalog": actual,
                    "actual_owned_increment": allocated(rig),
                }
            )
            disk_gate(rig, EXTRA_PEAK, "after-n3-" + stage)
        report["status"] = STATUS
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        try:
            cleanup(rig, report)
            if before is not None:
                report["catalog_after_cleanup"] = pg_snapshot(rig)
                assert report["catalog_after_cleanup"] == before, (
                    "N3 cleanup changed original catalog/data"
                )
        except Exception as error:
            report.update(status="FAIL", cleanup_error=str(error))
        data = json.dumps(report, indent=2).encode() + b"\n"
        assert (
            len(data)
            + sum(
                p.stat().st_size
                for p in (rig.root / "work").glob("n3-*.json")
                if p.name != "n3-compatibility.json"
            )
            <= PINS["report_bound"]
        )
        assert not any(s.encode() in data for s in rig.secrets.values()), (
            "N3 report secret exposure"
        )
        (rig.root / "work/n3-compatibility.json").write_bytes(data)
    assert report["status"] == STATUS, "N3 native/cleanup failure; receipts retained"
    return {"status": STATUS, "sha256": digest(data)}


def export(rig, disk_gate):
    """Bound both complete evidence copies and preserve raw-secret checks on failure."""
    files = sorted((rig.root / "work").glob("n3-*.json"))
    size = sum(p.stat().st_size for p in files)
    assert size <= PINS["report_bound"], "N3 aggregate original report bound"
    disk_gate(rig, size, "before-n3-evidence-export")
    for path in files:
        data = path.read_bytes()
        assert not any(s.encode() in data for s in rig.secrets.values()), (
            "N3 export secret exposure"
        )
        (rig.root / "evidence" / path.name).write_bytes(data)
    disk_gate(rig, 0, "after-n3-evidence-export")
