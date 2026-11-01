"""tests/integration/t44a_qualification/app_runtime.py

First real complete-app slice, using the frozen launcher and audited resource updates.
"""

import importlib
import json
import re
import secrets
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from app_build import build_outputs, verify_build_update
from run import PINS, disk_gate, download, image_present, sha

HERE = Path(__file__).resolve().parent
MIB = 1024**2
# Each service retains the frozen image/user/mount/security/network profile.
# These are running limits, not permission to overlap build or fixture production.
PROFILES = {
    "postgres": (0.5, 512, None),
    "fixture": (3, 3072, None),
    "garage": (0.25, 256, 128),
    "app": (2.25, 2560, 2048),
    "actor": (1, 1024, 640),
}


def active_host_config(config):
    """Docker reports enabled OOM killing as either false or null after start."""
    value = config["OomKillDisable"]
    assert value is False or value is None, "Active container OOM killer must remain enabled"
    normalized = dict(config)
    normalized["OomKillDisable"] = False
    return normalized


def runtime_rig(base):
    """Reuse launch/capture/cleanup; audit only exact owned nonrunning limit updates."""

    class RuntimeRig(base):
        component = "postgres"

        def create(self, role, command):
            expected_role = {"postgres": "postgres", "fixture": "producer"}.get(
                self.component, "consumer"
            )
            assert role == expected_role
            # The original create gate remains: only PG may be running here.
            name = super().create(role, command)
            before = json.loads(self.call(["docker", "inspect", name]).stdout)[0]
            cpus, memory, _ = PROFILES[self.component]
            self.call(
                [
                    "docker",
                    "update",
                    "--cpus=" + str(cpus),
                    "--memory=" + str(memory * MIB),
                    "--memory-swap=" + str(memory * MIB),
                    before["Id"],
                ]
            )
            after = json.loads(self.call(["docker", "inspect", before["Id"]]).stdout)[0]
            verify_build_update(before, after, cpus, memory * MIB)
            self.objects[name] = (self.component, after)
            return name

        def signature(self, role):
            return sha(
                (
                    super().signature(role)
                    + json.dumps(
                        [self.component, PROFILES[self.component], self.input_hashes],
                        sort_keys=True,
                    )
                ).encode()
            )

        def start_owned(self, name, attached=False):
            component, frozen = self.objects[name]
            self.component = component
            role = "postgres" if component == "postgres" else "consumer"
            if component != "postgres":
                assert self.ready[component] == self.signature(role), "STALE runtime readiness"
            now = json.loads(self.call(["docker", "inspect", name]).stdout)[0]
            cpus, memory, _ = PROFILES[component]
            verify_build_update(frozen, now, cpus, memory * MIB)
            if component != "postgres":
                self.shared_network(now["HostConfig"]["NetworkMode"])
            running = self.call(
                ["docker", "ps", "-q", "--no-trunc", "--filter", "label=probe.owner=" + self.label]
            ).stdout.split()
            known = {info["Id"]: (n, c) for n, (c, info) in self.objects.items()}
            active = []
            for identity in running:
                assert identity in known, "Unregistered running owned identity"
                other_name, other_component = known[identity]
                info = json.loads(self.call(["docker", "inspect", identity]).stdout)[0]
                assert info["Name"] == "/" + other_name
                assert info["Config"]["Labels"]["probe.owner"] == self.label
                assert (
                    info["Id"] == identity and info["Image"] == self.objects[other_name][1]["Image"]
                )
                assert info["State"]["Running"] and info["State"]["Status"] == "running"
                assert sorted(json.dumps(m, sort_keys=True) for m in info["Mounts"]) == sorted(
                    json.dumps(m, sort_keys=True) for m in self.objects[other_name][1]["Mounts"]
                )
                assert active_host_config(info["HostConfig"]) == active_host_config(
                    self.objects[other_name][1]["HostConfig"]
                ), "Active container HostConfig changed beyond Docker's enabled-OOM default"
                assert info["Config"] == self.objects[other_name][1]["Config"]
                active.append(other_component)
            active.append(component)
            assert len(active) == len(set(active)), "Duplicate running service role"
            assert sum(PROFILES[c][0] for c in active) <= 4
            assert sum(PROFILES[c][1] for c in active) <= 4608
            self.call(["docker", "start", *(["-a"] if attached else []), name], timeout=600)

    return RuntimeRig


def main():
    """Fresh synthetic fixture, real S3, normal migrations/app/CLI and ordinary actors."""
    root, baseline = [Path(p).resolve() for p in sys.argv[1:]]
    build = json.loads((root / "evidence/app-build.json").read_text())
    assert build["status"] == "PASS_COMPLETE_NORMAL_BUILD"
    assert build_outputs(root / "work/app/project") == build["outputs"]
    helper = baseline / "tests/integration/lightdash_probe"
    assert sha((helper / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    sys.path.insert(0, str(helper))
    runtime = importlib.import_module("runtime")
    generated = {
        k: secrets.token_hex(32) for k in ["pg", "appSecret", "s3Secret", "wrongS3", "garageRpc"]
    }
    generated.update(
        s3Access="GK" + secrets.token_hex(16),
        ownerPassword="Owner1" + secrets.token_hex(16),
        editorPassword="Editor1" + secrets.token_hex(16),
    )
    for value in generated.values():
        print("::add-mask::" + value, flush=True)
    rig = runtime_rig(runtime.Rig)(root, generated)
    rig.label = "t44a03-runtime-" + str(time.time_ns())
    rig.receipts = json.loads((root / "evidence/commands.json").read_text())
    rig.objects, rig.ready = {}, {}
    rig.input_hashes = {
        n: sha((HERE / n).read_bytes())
        for n in [
            "app_service.cjs",
            "app_actor.cjs",
            "app_runtime_smoke.cjs",
            "garage.toml",
            "diagnostic.py",
            "app-profile.json",
        ]
    }
    result = {"status": "FAIL", "full_app": "UNRUN", "fixture": "UNRUN", "profiles": PROFILES}
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

    def pg_snapshot():
        raw = rig.call(
            [
                "docker",
                "exec",
                rig.pg,
                "pg_dump",
                "--host=127.0.0.1",
                "--username=postgres",
                "--dbname=probe",
                "--no-owner",
                "--no-acl",
            ]
        ).stdout
        # PostgreSQL emits random psql restriction tokens on otherwise identical dumps.
        normalized = "\n".join(
            line
            for line in raw.splitlines()
            if not re.fullmatch(r"\\(?:un)?restrict [A-Za-z0-9]+", line)
        )
        files = {
            p.relative_to(root / "fixture/data").as_posix(): sha(p.read_bytes())
            for p in (root / "fixture/data").rglob("*.parquet")
        }
        assert files
        return {"catalog_dump_sha256": sha(normalized.encode()), "parquet": files}

    try:
        rig.docker_root = Path(
            json.loads(rig.call(["docker", "info", "--format", "{{json .DockerRootDir}}"]).stdout)
        )
        profile = json.loads((HERE / "app-profile.json").read_text())
        garage = profile["garage"]
        # Actual completed build is already in owned bytes. No future build copies charged again.
        # Fresh PG1GiB+512MiB WAL, dbt/spill512MiB, fresh driver expansion, Garage512MiB,
        # app/results1GiB, actor caches1GiB, four tmpfs/logs and PG socket tmpfs.
        driver = next(p for p in PINS["artifacts"] if p["kind"] == "driver")
        remaining = (1024 + 512 + 512 + 512 + 1024 + 1024 + 4 * 64 + 16 + 4 * 8) * MIB
        remaining += (
            driver["expanded_upper"] + garage["binary"]["bytes"] * 2 + garage["source"]["bytes"]
        )
        result["remaining_runtime_upper"] = remaining
        disk_gate(rig, remaining, "before-fresh-runtime-artifacts-images-and-fixture")
        for role, pin in PINS["images"].items():
            existing[role] = image_present(rig, pin["ref"])
            rig.inspect_image(role)
        old_names = {
            r["argv"][3]
            for r in rig.receipts
            if r.get("argv", [])[:3] == ["docker", "create", "--name"]
        }
        for name in old_names:
            missing = rig.call(["docker", "inspect", name], check=False)
            assert (
                missing.returncode == 1
                and missing.stderr.strip() == "Error: No such object: " + name
            )
        # Preserve prior owned data; the old credential was intentionally deleted.
        for old, new in [
            ("pgdata", "work/prior-pgdata"),
            ("fixture/data", "work/prior-fixture-data"),
            ("work/project", "work/prior-dbt-project"),
            ("home/adbc", "work/prior-adbc"),
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
        assert (root / "source/readiness").read_text() == "populated\n"
        for name in ["tools", "scripts", "secrets", "fixture", "fixture/data"]:
            install(root / "source/readiness", root / name / "readiness")
        for name in [
            "app_runtime_smoke.cjs",
            "app_service.cjs",
            "app_actor.cjs",
            "garage.toml",
            "diagnostic.py",
            "app-profile.json",
        ]:
            install(HERE / name, root / "scripts" / name)
        for artifact, url in [
            (garage["binary"], garage["binary_url"]),
            (garage["source"], garage["source_url"]),
        ]:
            data = download(url, artifact["sha256"], artifact["bytes"])
            path = root / "host-home" / artifact["sha256"]
            path.write_bytes(data)
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
        pg_smoke = rig.call(
            [
                "docker",
                "exec",
                rig.pg,
                "sh",
                "-c",
                'test "$(id -u)" = 999 && test "$(id -g)" = 999 '
                "&& test -w /var/lib/postgresql/data "
                '&& test "$(cat /sys/fs/cgroup/memory.max)" = 536870912 '
                '&& test "$(cat /sys/fs/cgroup/memory.swap.max)" = 0 '
                '&& test "$(cat /sys/fs/cgroup/cpu.max)" = "50000 100000" '
                "&& if touch /secrets/must-refuse 2>/dev/null; then exit 1; fi",
            ]
        )
        assert pg_smoke.returncode == 0
        rig.call(
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
                "-c",
                "CREATE DATABASE lightdash",
            ]
        )
        rig.component = "fixture"
        rig.run("producer", ["-c", "/work/venv/bin/python /scripts/smoke.py postgres"], smoke=True)
        rig.smokes["producer"] = rig.signature("producer")
        disk_gate(rig, remaining, "before-real-dbt-fresh-runtime-fixture")
        rig.run("producer", ["-c", "/work/venv/bin/python /scripts/diagnostic.py"], timeout=900)
        result["fixture"] = "PASS_REAL_DBT_WITH_COLUMN_METRIC_YAML"
        manifest = json.loads((root / "work/project/target/manifest.json").read_text())
        model = manifest["nodes"]["model.t44a_probe.orders"]
        assert set(model["columns"]) == {"id", "order_date", "amount"}
        assert model["columns"]["amount"]["meta"]["metrics"]["total_amount"]["type"] == "sum"
        result["authentic_dbt_artifacts"] = {
            n: sha((root / "work/project/target" / n).read_bytes())
            for n in ["manifest.json", "run_results.json", "catalog.json"]
        }
        result["catalog_before"] = pg_snapshot()
        for component in ["garage", "app", "actor"]:
            rig.component = component
            heap = PROFILES[component][2]
            command = "export NODE_OPTIONS=--max-old-space-size=" + str(heap) + "; "
            rig.run(
                "consumer",
                ["-c", command + "node /scripts/app_runtime_smoke.cjs " + component],
                smoke=True,
            )
            rig.ready[component] = rig.signature("consumer")
        names = {}
        # All three objects are created while only PG runs, retaining the baseline aggregate-create guard.
        for component in ["garage", "app", "actor"]:
            rig.component = component
            assert rig.ready[component] == rig.signature("consumer")
            command = (
                "export NODE_OPTIONS=--max-old-space-size=" + str(PROFILES[component][2]) + "; "
            )
            command += (
                "node /scripts/app_actor.cjs"
                if component == "actor"
                else "node /scripts/app_service.cjs " + component
            )
            names[component] = rig.create("consumer", ["-c", command])
        disk_gate(rig, remaining, "before-running-garage-and-full-app")
        rig.start_owned(names["garage"])
        rig.start_owned(names["app"])
        result["full_app"] = "STARTED_NOT_YET_ACCEPTED"
        rig.start_owned(names["actor"], attached=True)
        actor = json.loads((root / "work/app-slice.json").read_text())
        assert actor["status"] == "PASS_FIRST_RUNNING_APP_SLICE"
        result["catalog_after"] = pg_snapshot()
        assert result["catalog_before"] == result["catalog_after"], (
            "Read-only catalog/parquet changed"
        )
        disk_gate(rig, 0, "after-first-real-running-app-slice")
        result["status"] = "PASS_FIRST_RUNNING_APP_SLICE"
        result["full_app"] = "PASS_BUILD_MIGRATE_FRONTEND_CLI_ACTORS"
    except Exception as error:
        result["error"] = str(error)
    finally:
        try:
            candidate = root / "work/app-slice.json"
            if candidate.exists():
                data = candidate.read_bytes()
                assert not any(v.encode() in data for v in generated.values()), (
                    "Artifact secret exposure"
                )
                (root / "evidence/app-slice.json").write_bytes(data)
        except Exception as error:
            result["export_error"] = str(error)
            result["status"] = "FAIL"
        cleanup_errors = []
        try:
            rig.cleanup()
        except Exception as error:
            cleanup_errors.append(str(error))
        try:
            actor_home = root / "work/actor-home"
            if actor_home.exists():
                rig.call(["sudo", "rm", "-rf", "--", str(actor_home)])
        except Exception as error:
            cleanup_errors.append(str(error))
        for role, was_present in existing.items():
            try:
                ref = PINS["images"][role]["ref"]
                if not was_present and image_present(rig, ref):
                    assert not rig.call(
                        ["docker", "ps", "-aq", "--filter", "ancestor=" + ref]
                    ).stdout.strip()
                    rig.call(["docker", "image", "rm", ref])
                    assert not image_present(rig, ref)
            except Exception as error:
                cleanup_errors.append(str(error))
        result["cleanup"] = "FAIL" if cleanup_errors else "PASS"
        if cleanup_errors:
            result["cleanup_error"] = cleanup_errors
            result["status"] = "FAIL"
        result["secret_files"] = sum(p.is_file() for p in (root / "secrets").rglob("*"))
        result["seconds"] = time.monotonic() - rig.started
        text = json.dumps(result, indent=2)
        assert not any(v in text for v in generated.values())
        (root / "evidence/app-runtime.json").write_text(text + "\n")
    assert result["status"] == "PASS_FIRST_RUNNING_APP_SLICE", result.get("error")


if __name__ == "__main__":
    main()
