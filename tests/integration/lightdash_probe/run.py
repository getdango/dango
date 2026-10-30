"""tests/integration/lightdash_probe/run.py

real Lightdash client feasibility on Ubuntu.
"""

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import sys
import time
from pathlib import Path

from acquire import bytes_used, disk_gate, extensions, handoff, packages, source, tools
from runtime import GIB, HERE, Rig


def readiness(rig, role):
    """Run the populated access/authenticated-network oracle using actual profiles."""
    rig.run(role, ["-c", "node /scripts/smoke.cjs " + role], timeout=45, smoke=True)
    rig.smokes[role] = rig.signature(role)
    rig.require_smoke(role)


def snapshot(rig):
    """Hash whole catalog schema/data and every fixture byte without hiding changes."""
    dump = rig.call(
        [
            "docker",
            "exec",
            "--user=999:999",
            rig.pg,
            "pg_dump",
            "--host=127.0.0.1",
            "--username=postgres",
            "--dbname=probe",
        ]
    ).stdout
    lines = dump.splitlines(keepends=True)
    normalized = "".join(
        line for line in lines if not re.fullmatch(r"\\(?:un)?restrict [A-Za-z0-9]+\n?", line)
    )
    metadata = rig.call(
        [
            "docker",
            "exec",
            "--user=999:999",
            rig.pg,
            "psql",
            "-h",
            "127.0.0.1",
            "-U",
            "postgres",
            "-d",
            "probe",
            "-Atc",
            "SELECT value FROM ducklake_metadata WHERE key='version'",
        ]
    ).stdout.strip()
    assert metadata == "1.1"
    files = {
        str(p.relative_to(rig.root / "fixture")): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (rig.root / "fixture").rglob("*")
        if p.is_file()
    }
    assert any(n.endswith(".parquet") for n in files), "Fixture must be materialized Parquet"
    return {
        "raw_dump_sha256": hashlib.sha256(dump.encode()).hexdigest(),
        "normalized_dump_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        "normalization": "Only exact pg_dump restrict/unrestrict random-token lines",
        "catalog_spec": metadata,
        "fixture_files": files,
    }


def execute(root):
    """Perform smoke, measured acquisition, offline build and actual-client oracles."""
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    assert not root.exists(), "Owned scratch must be fresh"
    root.mkdir(mode=0o700)
    for name in [
        "source",
        "tools",
        "scripts",
        "work",
        "home",
        "fixture",
        "pgdata",
        "secrets",
        "evidence",
    ]:
        (root / name).mkdir(mode=0o755)
        (root / name / "readiness").write_text("populated\n")
    shutil.copytree(
        HERE, root / "scripts", dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
    )
    (root / "fixture/data").mkdir()
    (root / "fixture/data/readiness").write_text("populated\n")
    for relative in [
        "packages/warehouses/src/warehouseClients/DuckdbWarehouseClient.ts",
        "packages/common/src/index.ts",
    ]:
        path = root / "source" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("populated\n")
    values = {
        key: secrets.token_hex(24) for key in ["adminPassword", "readerPassword", "wrongPassword"]
    }
    for value in values.values():
        print("::add-mask::" + value, flush=True)
    rig = Rig(root, values)
    result = {
        "status": "FAIL",
        "native": "UNRUN",
        "head": os.environ.get("GITHUB_SHA"),
        "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runner": {"os": platform.platform(), "architecture": platform.machine()},
    }
    try:
        info = json.loads(rig.call(["docker", "info", "--format", "{{json .}}"]).stdout)
        assert info["NCPU"] >= 4 and info["MemTotal"] >= 4 * GIB
        assert info["OSType"] == "linux"
        rig.call(["sudo", "chown", "999:999", str(root / "pgdata")])
        rig.call(["sudo", "chmod", "700", str(root / "pgdata")])
        # Postgres requires PGDATA to start empty; its actual writable path is tested by initdb.
        rig.call(["sudo", "rm", "--", str(root / "pgdata/readiness")])
        for name, data, owner in [
            ("catalog.json", json.dumps(values), "501:20"),
            ("admin", values["adminPassword"], "999:999"),
            ("pgpass", "127.0.0.1:5432:probe:postgres:" + values["adminPassword"], "999:999"),
        ]:
            p = root / "secrets" / name
            p.write_text(data)
            p.chmod(0o600)
            rig.call(["sudo", "chown", owner, str(p)])
        disk_gate(rig, 2 * GIB, "before-frozen-image-acquisition")
        for role in ["node", "postgres"]:
            rig.inspect_image(role)
        tools(rig)
        # Tiny, separately pinned harness dependency only; no Lightdash staging/build yet.
        shutil.copytree(HERE / "smoke", root / "tools/smoke")
        rig.call(
            [
                "node",
                str(root / "tools/npm/bin/npm-cli.js"),
                "ci",
                "--ignore-scripts",
                "--omit=dev",
                "--audit=false",
                "--fund=false",
                "--prefix",
                str(root / "tools/smoke"),
                "--cache",
                str(root / "work/npm-cache"),
            ],
            timeout=120,
        )
        rig.call(["chmod", "-R", "a+rX", str(root / "tools")])
        handoff(rig, ["source", "tools", "scripts", "work", "home", "fixture"], runtime=True)
        readiness(rig, "installer")
        rig.pg = rig.create(
            "postgres",
            [
                "postgres",
                "-c",
                "listen_addresses=127.0.0.1",
                "-c",
                "password_encryption=scram-sha-256",
            ],
        )
        rig.call(["docker", "start", rig.pg])
        assert rig.call(["docker", "exec", rig.pg, "id", "-u"]).stdout.strip() == "999"
        assert rig.call(["docker", "exec", rig.pg, "id", "-g"]).stdout.strip() == "999"
        readiness(rig, "producer")
        readiness(rig, "consumer")
        rig.call(["docker", "stop", "--time=10", rig.pg])
        result["smoke"] = "PASS"
        source(rig)
        readiness(rig, "installer")
        rig.run("installer", ["-c", "sh /scripts/build.sh lock"])
        packages(rig)
        extensions(rig)
        handoff(rig, ["work"], runtime=True)
        disk_gate(rig, rig.install_remaining, "immediately-before-offline-build")
        readiness(rig, "installer")
        rig.run("installer", ["-c", "sh /scripts/build.sh build"])
        result["install_compile"] = "PASS"
        compiler = json.loads((root / "work/compiler.json").read_text())
        assert any(p.endswith("packages/common/src/index.ts") for p in compiler["inputs"])
        assert any(p.endswith("DuckdbWarehouseClient.ts") for p in compiler["inputs"])
        (root / "evidence/compiler.json").write_text(json.dumps(compiler, indent=2))
        rig.call(["docker", "start", rig.pg])
        readiness(rig, "producer")
        readiness(rig, "consumer")
        scenario = time.monotonic()
        result["native"] = "INVOKED"
        rig.run("consumer", ["-c", "node /scripts/native.cjs"], timeout=45)
        result["native"] = "RAN"
        rig.run("producer", ["-c", "node /scripts/fixture.cjs"], timeout=120)
        before = snapshot(rig)
        rig.run("consumer", ["-c", "node /work/deploy/client.cjs positive"], timeout=120)
        rig.run("consumer", ["-c", "node /work/deploy/client.cjs wrong-credential"], timeout=120)
        rig.run("consumer", ["-c", "node /work/deploy/client.cjs positive"], timeout=120)
        after = snapshot(rig)
        assert before["normalized_dump_sha256"] == after["normalized_dump_sha256"]
        assert before["fixture_files"] == after["fixture_files"]
        assert time.monotonic() - scenario <= 2700
        result.update(
            status="PASS", before=before, after=after, scenario_seconds=time.monotonic() - scenario
        )
    except Exception as error:
        message = str(error)
        assert not any(value in message for value in values.values()), (
            "Secret-bearing exception withheld"
        )
        result["error"] = message
    finally:
        try:
            rig.cleanup()
            result["cleanup"] = "PASS"
        except Exception as error:
            result.update(status="FAIL", cleanup=str(error))
        result["remaining_secret_files"] = len(list((root / "secrets").iterdir()))
        result["owned_allocated_bytes"] = bytes_used(root)
        result["seconds"] = time.monotonic() - rig.started
        result["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        output = json.dumps(result, indent=2)
        assert not any(value in output for value in values.values())
        (root / "evidence/result.json").write_text(output + "\n")
        rig.save()
    print(
        json.dumps(
            {
                "status": result["status"],
                "native": result["native"],
                "cleanup": result.get("cleanup"),
            }
        )
    )
    return (
        0
        if result["status"] == result.get("cleanup") == "PASS"
        and not result["remaining_secret_files"]
        else 1
    )


if __name__ == "__main__":
    sys.exit(execute(Path(sys.argv[1]).resolve()))
