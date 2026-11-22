"""tests/integration/t44a_qualification/engine_runtime.py

N2 selected clients and engine S3 only, reusing the reviewed launch/cleanup controller.
"""

import importlib
import json
import secrets
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import engine_behavior as engine
from app_build import build_outputs
from app_runtime import PROFILES, runtime_rig
from held_reader import pg_snapshot
from run import PINS, disk_gate, download, image_present, sha
from runtime_cleanup import cleanup_runtime_storage

HERE = Path(__file__).resolve().parent
MIB = 1024**2


def run_owned(rig, name):
    """Start a precreated exact object; capture and remove it before the next profile."""
    try:
        rig.start_owned(name)
        result = rig.call(["docker", "wait", rig.objects[name][1]["Id"]], timeout=900)
        assert result.stdout.strip() == "0", "Native engine child failed; see retained capture"
    finally:
        try:
            rig.capture(name)
        finally:
            rig.remove(name)


def main():
    """Run a single measured N2 slice without full app or notebook services."""
    root, baseline = [Path(p).resolve() for p in sys.argv[1:]]
    assert engine.reserve(root) == engine.EXTRA_PEAK
    engine.require_early(root)
    build = json.loads((root / "evidence/app-build.json").read_text())
    assert build["status"] == "PASS_NORMAL_SELECTED_CLIENT_BUILD"
    assert build_outputs(root / "work/app/project", True) == build["outputs"]
    base = baseline / "tests/integration/lightdash_probe"
    assert sha((base / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    sys.path.insert(0, str(base))
    runtime = importlib.import_module("runtime")
    generated = {name: secrets.token_hex(32) for name in ["pg", "s3Secret", "wrongS3", "garageRpc"]}
    generated["s3Access"] = "GK" + secrets.token_hex(16)
    for value in generated.values():
        print("::add-mask::" + value, flush=True)
    rig = runtime_rig(runtime.Rig)(root, generated)
    rig.label = "t44a03-engine-" + str(time.time_ns())
    rig.receipts = json.loads((root / "evidence/commands.json").read_text())
    rig.objects, rig.ready = {}, {}
    files = [
        *engine.FILES,
        "engine_runtime.py",
        "app_runtime.py",
        "app_runtime_smoke.cjs",
        "app_service.cjs",
        "garage.toml",
        "runtime_cleanup.py",
        "diagnostic.py",
        "app-profile.json",
    ]
    rig.input_hashes = {name: sha((HERE / name).read_bytes()) for name in files}
    report = {
        "status": "FAIL",
        "full_app": "UNRUN_SELECTED_CLIENT_ONLY",
        "profiles": {name: PROFILES[name] for name in ["postgres", "fixture", "garage", "actor"]},
        "inputs": rig.input_hashes,
        "session_limit": "Node inventory and blob are separate runQuery sessions",
    }
    existing = {}

    def install(source, target, uid=501, gid=20, mode="644"):
        rig.call(
            [
                "sudo",
                "install",
                "-m",
                mode,
                "-o",
                str(uid),
                "-g",
                str(gid),
                str(source),
                str(target),
            ]
        )

    def secret_file(name, body, uid=501, gid=20):
        with tempfile.NamedTemporaryFile(dir=root / "host-home", delete=False) as temp:
            path = Path(temp.name)
            temp.write(body.encode())
        try:
            install(path, root / "secrets" / name, uid, gid, "600")
        finally:
            path.unlink()

    try:
        rig.docker_root = Path(
            json.loads(rig.call(["docker", "info", "--format", "{{json .DockerRootDir}}"]).stdout)
        )
        garage = json.loads((HERE / "app-profile.json").read_text())["garage"]
        driver = next(p for p in PINS["artifacts"] if p["kind"] == "driver")
        # Fresh PG/WAL and genuine dbt+dlt setup remain explicit, not confused with completed build bytes.
        remaining = engine.EXTRA_PEAK + (1024 + 512 + 512 + 3 * 64 + 16 + 4 * 8) * MIB
        remaining += (
            driver["expanded_upper"] + 2 * garage["binary"]["bytes"] + garage["source"]["bytes"]
        )
        report["remaining_runtime_upper"] = remaining
        disk_gate(rig, remaining, "before-engine-runtime-artifacts-and-fresh-fixture")
        for role, pin in PINS["images"].items():
            existing[role] = image_present(rig, pin["ref"])
            rig.inspect_image(role)
        old_names = {
            r["argv"][3]
            for r in rig.receipts
            if r.get("argv", [])[:3] == ["docker", "create", "--name"]
        }
        for name in old_names:
            absent = rig.call(["docker", "inspect", name], check=False)
            assert (
                absent.returncode == 1 and absent.stderr.strip() == "Error: No such object: " + name
            )
        for old, new in [
            ("pgdata", "work/prior-pgdata"),
            ("fixture/data", "work/prior-fixture-data"),
            ("work/project", "work/prior-dbt-project"),
            ("home/adbc", "work/prior-adbc"),
            ("work/dlt-data", "work/prior-dlt-data"),
            ("work/dlt-project", "work/prior-dlt-project"),
        ]:
            assert (root / old).is_dir() and not (root / new).exists()
            rig.call(["sudo", "mv", str(root / old), str(root / new)])
        for name, uid, gid, mode in [
            ("pgdata", 999, 999, "700"),
            ("fixture/data", 501, 20, "755"),
            ("work/garage", 501, 20, "700"),
            ("work/actor-home", 501, 20, "700"),
        ]:
            rig.call(
                [
                    "sudo",
                    "install",
                    "-d",
                    "-m",
                    mode,
                    "-o",
                    str(uid),
                    "-g",
                    str(gid),
                    str(root / name),
                ]
            )
        for name in ["tools", "scripts", "secrets", "fixture", "fixture/data"]:
            install(root / "source/readiness", root / name / "readiness")
        for name in files:
            install(HERE / name, root / "scripts" / name)
        inventory_data = (root / "evidence/app-package-inventory.json").read_bytes()
        assert (
            sha(inventory_data)
            == json.loads((root / "evidence/app-acquisition.json").read_text())["inventory_sha256"]
        )
        install(
            root / "evidence/app-package-inventory.json",
            root / "work/engine-package-inventory.json",
        )
        for artifact, url in [
            (garage["binary"], garage["binary_url"]),
            (garage["source"], garage["source_url"]),
        ]:
            body = download(url, artifact["sha256"], artifact["bytes"])
            path = root / "host-home" / artifact["sha256"]
            path.write_bytes(body)
            if artifact is garage["binary"]:
                install(path, root / "tools/garage", mode="555")
            else:
                with tarfile.open(path) as archive:
                    member = next(
                        m for m in archive.getmembers() if m.name.split("/", 1)[-1] == "LICENSE"
                    )
                    notice = archive.extractfile(member).read()
                    assert sha(notice) == garage["license_file"]["sha256"]
                    (root / "evidence/garage-LICENSE.txt").write_bytes(notice)
        secret_file("app.json", json.dumps(generated))
        secret_file("catalog.json", json.dumps({"adminPassword": generated["pg"]}))
        secret_file("admin", generated["pg"], 999, 999)
        secret_file("pgpass", "127.0.0.1:5432:*:postgres:" + generated["pg"] + "\n", 999, 999)
        secret_file("pgpass-python", "127.0.0.1:5432:*:postgres:" + generated["pg"] + "\n")
        secret_file("garage-rpc", generated["garageRpc"])
        rig.pg = rig.create("postgres", ["postgres", "-c", "max_wal_size=128MB"])
        rig.start_owned(rig.pg)
        deadline = time.monotonic() + 90
        while rig.call(
            ["docker", "exec", rig.pg, "pg_isready", "-h", "127.0.0.1"], check=False
        ).returncode:
            assert time.monotonic() < deadline
            time.sleep(0.25)
        rig.call(
            [
                "docker",
                "exec",
                rig.pg,
                "sh",
                "-c",
                'test "$(id -u)" = 999 && test "$(id -g)" = 999 '
                '&& test "$(cat /sys/fs/cgroup/memory.max)" = 536870912 '
                '&& test "$(cat /sys/fs/cgroup/memory.swap.max)" = 0 '
                '&& test "$(cat /sys/fs/cgroup/cpu.max)" = "50000 100000" '
                "&& test -w /var/lib/postgresql/data && if touch /secrets/must-refuse 2>/dev/null; then exit 1; fi",
            ]
        )
        rig.component = "fixture"
        rig.run("producer", ["-c", "/work/venv/bin/python /scripts/smoke.py postgres"], smoke=True)
        rig.smokes["producer"] = rig.signature("producer")
        rig.ready["fixture"] = rig.signature("producer")
        disk_gate(rig, remaining, "before-fresh-engine-dbt-producer-fixture")
        rig.run(
            "producer",
            [
                "-c",
                "/work/venv/bin/python /scripts/diagnostic.py && /work/venv/bin/python /scripts/producer_capture.py",
            ],
            timeout=900,
        )
        capture = root / "work/producer-capture.json"
        assert (
            json.loads(capture.read_text())["status"] == "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE"
        )
        install(capture, root / "work/engine-fresh-producer-capture.json")
        report["catalog_before"] = pg_snapshot(rig)
        for component in ["garage", "actor"]:
            rig.component = component
            command = (
                "export NODE_OPTIONS=--max-old-space-size=" + str(PROFILES[component][2]) + "; "
            )
            rig.run(
                "consumer",
                ["-c", command + "node /scripts/app_runtime_smoke.cjs " + component],
                smoke=True,
            )
            rig.ready[component] = rig.signature("consumer")
        names = {}
        # All objects are created while only PG runs. The unchanged start gate audits every live peer.
        for label, component, command in [
            ("garage", "garage", "node /scripts/app_service.cjs garage"),
            ("seed", "actor", "node /scripts/engine_client.cjs seed"),
            ("node", "actor", "node /scripts/engine_client.cjs native"),
            ("python", "fixture", "/work/venv/bin/python /scripts/engine_behavior.py s3"),
            ("delete", "actor", "node /scripts/engine_client.cjs delete"),
        ]:
            rig.component = component
            if component != "fixture":
                command = (
                    "export NODE_OPTIONS=--max-old-space-size="
                    + str(PROFILES[component][2])
                    + "; "
                    + command
                )
            names[label] = rig.create(
                "producer" if component == "fixture" else "consumer", ["-c", command]
            )
        disk_gate(rig, remaining, "before-engine-garage-and-selected-clients")
        rig.start_owned(names["garage"])
        for label in ["seed", "node", "python", "delete"]:
            disk_gate(rig, remaining, "before-engine-" + label)
            run_owned(rig, names[label])
        for name, status in [
            ("engine-node.json", "PASS_SELECTED_LIGHTDASH_ENGINE_AND_HTTPFS_CASES"),
            ("engine-python-s3.json", "PASS_PYTHON_HTTPFS_SECRET_GOOD_BAD_GOOD"),
        ]:
            data = (root / "work" / name).read_bytes()
            assert json.loads(data)["status"] == status
            report[name] = sha(data)
        report["catalog_after"] = pg_snapshot(rig)
        assert report["catalog_before"] == report["catalog_after"], (
            "Engine probes mutated catalog/data"
        )
        report["status"] = "PASS_N2_SELECTED_ENGINE_CASES"
    except Exception as error:
        report["error"] = str(error)
    finally:
        try:
            engine.export(rig, generated, disk_gate)
        except Exception as error:
            report.update(status="FAIL", export_error=str(error))
        errors = []
        for action in [
            rig.cleanup,
            lambda: report.update(runtime_storage_cleanup=cleanup_runtime_storage(rig)),
        ]:
            try:
                action()
            except Exception as error:
                errors.append(str(error))
        for role, present in existing.items():
            try:
                ref = PINS["images"][role]["ref"]
                if not present and image_present(rig, ref):
                    assert not rig.call(
                        ["docker", "ps", "-aq", "--filter", "ancestor=" + ref]
                    ).stdout.strip()
                    rig.call(["docker", "image", "rm", ref])
                    assert not image_present(rig, ref)
            except Exception as error:
                errors.append(str(error))
        report["cleanup"] = "FAIL" if errors else "PASS"
        if errors:
            report.update(status="FAIL", cleanup_errors=errors)
        report["secret_files"] = sum(p.is_file() for p in (root / "secrets").rglob("*"))
        if report["secret_files"]:
            report["status"] = "FAIL"
        try:
            disk_gate(rig, 0, "after-engine-runtime-and-evidence-cleanup")
        except Exception as error:
            report.update(status="FAIL", resource_error=str(error))
        report["seconds"] = time.monotonic() - rig.started
        text = json.dumps(report, indent=2)
        assert len(text.encode()) <= engine.CONTRACT["limits"]["report"]
        assert not any(value in text for value in generated.values())
        (root / "evidence/engine-runtime.json").write_text(text + "\n")
    assert report["status"] == "PASS_N2_SELECTED_ENGINE_CASES", report.get("error")


if __name__ == "__main__":
    main()
