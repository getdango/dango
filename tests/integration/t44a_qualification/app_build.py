"""tests/integration/t44a_qualification/app_build.py

Offline full install without lifecycles, then complete normal upstream builds.
"""

import collections
import importlib
import json
import sys
import time
import urllib.request
from pathlib import Path

from app_acquire import ACQUIRE, HOOK, YAML_SHA, critical
from run import PINS, disk_gate, image_present, sha

HERE = Path(__file__).resolve().parent
GIB = 1024**3
BUILD_MEMORY = 4608 * 1024**2
GO_MEMORY = "3500MiB"
NODE_MEMORY = "--max-old-space-size=3500"
BUILD_OUTPUT_TEMP = 8 * GIB
BUILD_COMMANDS = [
    "formula:build",
    "common-build",
    "warehouses-build",
    "visualization-build",
    "backend-build",
    "frontend-build",
    "cli-build",
]


def verify_build_update(before, after):
    """Permit only the approved CPU/memory update on the same unstarted object."""
    expected = dict(before["HostConfig"])
    expected.update(NanoCpus=4_000_000_000, Memory=BUILD_MEMORY, MemorySwap=BUILD_MEMORY)
    assert after["HostConfig"] == expected, "Unexpected build profile or security change"
    for key in ["Id", "Name", "Image", "Config", "State"]:
        assert after[key] == before[key], "Unexpected container identity/config/state change"
    # Docker may reorder mounts on update; compare every field and retain duplicates.
    assert sorted(json.dumps(m, sort_keys=True) for m in after["Mounts"]) == sorted(
        json.dumps(m, sort_keys=True) for m in before["Mounts"]
    ), "Unexpected container mount change"
    assert after["State"]["Status"] == "created" and not after["State"]["Running"]


def build_rig(base):
    """Keep immutable launch validation, then explicitly update and reverify limits."""

    class BuildRig(base):
        """Only the installer role may use the approved larger resource profile."""

        def create(self, role, command):
            """Validate original flags first; update exact owned ID before start."""
            assert role == "installer", "No background service in build profile"
            name = super().create(role, command)
            before = json.loads(self.call(["docker", "inspect", name]).stdout)[0]
            assert before["Name"] == "/" + name
            assert before["Config"]["Labels"]["probe.owner"] == self.label
            assert before["State"]["Status"] == "created" and not before["State"]["Running"]
            self.call(
                [
                    "docker",
                    "update",
                    "--cpus=4",
                    "--memory=" + str(BUILD_MEMORY),
                    "--memory-swap=" + str(BUILD_MEMORY),
                    before["Id"],
                ]
            )
            after = json.loads(self.call(["docker", "inspect", before["Id"]]).stdout)[0]
            verify_build_update(before, after)
            return name

        def signature(self, role):
            """Bind readiness to the original paths/flags plus declared update/Go limit."""
            return sha(
                (
                    super().signature(role) + str(BUILD_MEMORY) + "4CPU" + GO_MEMORY + NODE_MEMORY
                ).encode()
            )

    return BuildRig


def with_memory_receipt(command):
    """Read own cgroup counters before PID1 exits, preserving original command status."""
    return (
        command + "; code=$?; for metric in memory.max memory.swap.max memory.peak "
        'memory.events cpu.max; do printf "CGROUP %s\\n" "$metric"; '
        'cat "/sys/fs/cgroup/$metric"; done; exit "$code"'
    )


def licenses(project, inventory, destination):
    """Bind the two missing declarations to exact published/source notice bytes."""
    pins = json.loads((HERE / "license-provenance.json").read_text())
    assert set(inventory["missing_declared_license"]) == set(pins)
    rows = {row["package"]: row for row in inventory["packages"]}
    retained = {}
    for key, pin in pins.items():
        assert rows[key]["sha256"] == pin["archive_sha256"]
        name, _ = key.split("@")
        package = project / "node_modules/.pnpm" / key / "node_modules" / name
        if "implementation_member" in pin:
            assert (
                sha((package / pin["implementation_member"]).read_bytes())
                == pin["implementation_sha256"]
            )
        if "notice_member" in pin:
            body = (package / pin["notice_member"]).read_bytes()
            assert len(body) == pin["notice_bytes"] and sha(body) == pin["notice_sha256"]
            assert b"The MIT License" in body
            (destination / pin["notice_sha256"]).write_bytes(body)
        for notice in pin.get("notices", []):
            assert notice["url"].startswith(
                "https://raw.githubusercontent.com/bitpay/node-buffers/"
                + pin["source_commit"]
                + "/"
            )
            with urllib.request.urlopen(notice["url"], timeout=60) as response:
                body = response.read(notice["bytes"] + 1)
            assert len(body) == notice["bytes"] and sha(body) == notice["sha256"]
            assert b"MIT/X11" in body
            (destination / notice["sha256"]).write_bytes(body)
        retained[key] = pin
    return retained


def build_remaining(inventory, lock, cache_bytes):
    """Reserve independent full materialization copies, cache rewrites and emit/temp."""
    snapshots = collections.Counter(k.split("(", 1)[0] for k in lock["snapshots"])
    virtual = sum(
        row["allocated_bound"] * max(1, snapshots[row["package"]]) for row in inventory["packages"]
    )
    # Existing content/virtual store is in actual owned bytes. No hardlink credit
    # for two further materialization/staging copies or two cache rewrite copies.
    # Reserve8GiB:4GiB retained full build outputs/maps/tsbuildinfo plus4GiB
    # compiler/plugin cache and temporary output copies (not a measured build size).
    # Add the bounded64MiB tmpfs and8MiB log. Separate2GiB engineering headroom is added
    # by disk_gate, before install and again before EACH actual compiler command.
    return virtual * 2 + cache_bytes * 2 + BUILD_OUTPUT_TEMP + (64 + 8) * 1024**2


def build_outputs(project):
    """Bind complete emitted trees without publishing compiled application bytes."""
    trees = {}
    for name in ["formula", "common", "warehouses", "visualization", "backend", "frontend", "cli"]:
        directory = project / "packages" / name / ("build" if name == "frontend" else "dist")
        rows = []
        allocated = 0
        for path in sorted(directory.rglob("*")):
            assert not path.is_symlink(), "Unexpected build output symlink"
            if path.is_file():
                body = path.read_bytes()
                rows.append([path.relative_to(directory).as_posix(), len(body), sha(body)])
                allocated += path.stat().st_blocks * 512
        assert rows, "Missing build output tree: " + name
        trees[name] = {
            "files": len(rows),
            "bytes": sum(row[1] for row in rows),
            "allocated_bytes": allocated,
            "inventory_sha256": sha(json.dumps(rows, separators=(",", ":")).encode()),
        }
    entries = [
        "visualization/dist/cjs/index.js",
        "visualization/dist/esm/index.js",
        "backend/dist/index.js",
        "backend/dist/generated/routes.js",
        "backend/dist/scripts/migrate/index.js",
        "frontend/build/index.html",
        "frontend/build/build-hash.json",
        "cli/dist/index.js",
    ]
    entry_hashes = {}
    for name in entries:
        body = (project / "packages" / name).read_bytes()
        assert body, "Empty build entrypoint: " + name
        entry_hashes[name] = sha(body)
    assets = project / "packages/frontend/build/assets"
    assert any(assets.rglob("*.js")) and any(assets.rglob("*.css")), "Missing frontend assets"
    assert sum(max(tree["bytes"], tree["allocated_bytes"]) for tree in trees.values()) <= 4 * GIB, (
        "Build outputs exceed allowance"
    )
    return {"trees": trees, "entrypoints_sha256": entry_hashes}


def main():
    """Reuse the original bounded installer and capture before exact owned cleanup."""
    root, baseline = [Path(p).resolve() for p in sys.argv[1:]]
    previous = json.loads((root / "evidence/app-acquisition.json").read_text())
    assert previous["status"] == "PASS_FULL_DEV_ACQUISITION_ONLY"
    data = (root / "evidence/app-package-inventory.json").read_bytes()
    assert sha(data) == previous["inventory_sha256"]
    inventory = json.loads(data)
    base = baseline / "tests/integration/lightdash_probe"
    assert sha((base / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    assert sha((base / "acquire.py").read_bytes()) == ACQUIRE
    sys.path.insert(0, str(base))
    runtime = importlib.import_module("runtime")
    rig = build_rig(runtime.Rig)(root, {})
    rig.label = "t44a03-build-" + str(time.time_ns())
    rig.receipts = json.loads((root / "evidence/commands.json").read_text())
    rig.docker_root = Path(
        json.loads(rig.call(["docker", "info", "--format", "{{json .DockerRootDir}}"]).stdout)
    )
    started = time.monotonic()
    result = {"status": "FAIL", "install": "UNRUN", "build": {}, "full_app": "UNRUN"}
    existing = True
    try:
        assert not any(p.is_file() for p in (root / "secrets").rglob("*"))
        (root / "secrets/readiness").write_text("populated\n")
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
        app = root / "app-acquisition"
        project = app / "project"
        assert sha((app / "pyyaml.whl").read_bytes()) == YAML_SHA
        sys.path.insert(0, str(app / "pyyaml.whl"))
        acquire = importlib.import_module("acquire")
        assert str(app / "pyyaml.whl") in sys.modules["yaml"].__file__
        lock = acquire.frozen_project_lock((project / "pnpm-lock.yaml").read_text())
        before = critical(project, lock)
        assert before[".pnpmfile.cjs"] == HOOK
        for name, pin in acquire.PINS["source"].items():
            assert sha((project / name).read_bytes()) == pin["after"]
        source_before = {
            name: sha((project / name).read_bytes()) for name in acquire.PINS["source"]
        }
        result["source_before"] = source_before
        remaining = build_remaining(inventory, lock, acquire.bytes_used(app / "pnpm-cache"))
        result["remaining_upper"] = remaining
        result["output_temp_allowance"] = BUILD_OUTPUT_TEMP
        disk_gate(rig, remaining, "before-offline-full-install-license-and-staging")
        resolutions = licenses(project, inventory, app / "notices")
        (root / "evidence/license-resolutions.json").write_text(
            json.dumps(resolutions, indent=2) + "\n"
        )
        compiler = next(
            r
            for r in inventory["packages"]
            if r["package"] == "@typescript/typescript-linux-x64@7.0.2"
        )
        assert compiler["native"]["package/lib/tsc"]["sha256"] == (
            "4f2de678286401759b3fb4475bafe35b8f32b4b3a07d92642bbf37eadc9b34a4"
        )
        compiler_path = (
            project
            / "node_modules/.pnpm/@typescript+typescript-linux-x64@7.0.2/node_modules/@typescript/typescript-linux-x64/lib/tsc"
        )
        assert sha(compiler_path.read_bytes()) == compiler["native"]["package/lib/tsc"]["sha256"]
        result["compiler_native"] = compiler["native"]["package/lib/tsc"]
        target = root / "work/app"
        tool = root / "tools/app-pnpm"
        assert not target.exists() and not tool.exists()
        rig.call(["sudo", "mv", str(app / "pnpm"), str(tool)])
        for name in ["pnpm-lock.yaml", ".pnpmfile.cjs"]:
            rig.call(["sudo", "cp", str(project / name), str(root / "source" / name)])
        rig.call(["sudo", "mv", str(app), str(target)])
        rig.call(["sudo", "chown", "-R", "501:20", str(target), str(tool), str(root / "source")])
        rig.call(
            [
                "sudo",
                "install",
                "-m",
                "644",
                "-o",
                "501",
                "-g",
                "20",
                str(HERE / "app_build_smoke.cjs"),
                str(root / "scripts/app_build_smoke.cjs"),
            ]
        )
        existing = image_present(rig, PINS["images"]["node"]["ref"])
        rig.inspect_image("node")
        environment = (
            "export GOMEMLIMIT="
            + GO_MEMORY
            + " NODE_OPTIONS="
            + NODE_MEMORY
            + " PATH=/tools/app-pnpm:$PATH XDG_CACHE_HOME=/home/probe/cache XDG_CONFIG_HOME=/home/probe/config XDG_DATA_HOME=/home/probe/data PNPM_CONFIG_CACHE_DIR=/work/app/pnpm-cache; "
        )
        rig.run(
            "installer",
            ["-c", environment + "node /scripts/app_build_smoke.cjs"],
            smoke=True,
        )
        rig.smokes["installer"] = rig.signature("installer")
        result["running_build_profile_smoke"] = "PASS"
        pnpm = "/tools/app-pnpm/pnpm --dir /work/app/project"
        disk_gate(rig, remaining, "before-offline-full-install-no-lifecycles")
        rig.run(
            "installer",
            [
                "-c",
                with_memory_receipt(
                    environment
                    + pnpm
                    + " install --offline --frozen-lockfile --ignore-scripts --store-dir /work/app/store --config.manage-package-manager-versions=false"
                ),
            ],
        )
        result["install"] = "PASS_OFFLINE_FULL_NO_LIFECYCLES"
        project = target / "project"
        assert critical(project, lock) == before
        for command in BUILD_COMMANDS:
            disk_gate(rig, remaining, "before-normal-" + command)
            build = pnpm + " run " + command
            if command == "backend-build":
                # Preserve all upstream stages; documented flag bounds both formatter pools.
                build = " && ".join(
                    pnpm + " -F backend " + stage
                    for stage in [
                        "run generate-api",
                        "run formatter --threads 1 --write ./src/generated",
                        "exec tsc --build tsconfig.build.json --singleThreaded",
                        "run postbuild",
                    ]
                )
            result.setdefault("build_commands", {})[command] = build
            rig.run("installer", ["-c", with_memory_receipt(environment + build)])
            result["build"][command] = "PASS"
            assert critical(project, lock) == before
            assert {
                name: sha((project / name).read_bytes()) for name in source_before
            } == source_before
        result["outputs"] = build_outputs(project)
        disk_gate(rig, 0, "after-complete-normal-app-build")
        result["status"] = "PASS_COMPLETE_NORMAL_BUILD"
        result["full_app"] = "BUILD_PASS_RUNTIME_UNRUN"
        result["lifecycles"] = (
            "Dependency install lifecycles unexecuted; normal upstream build commands and their pre/post hooks execute offline. No canvas/Cypress/legacy DuckDB install/rebuild. App runtime native initialization remains UNRUN."
        )
    except Exception as error:
        result["error"] = str(error)
    finally:
        try:
            rig.cleanup()
            ref = PINS["images"]["node"]["ref"]
            if not existing:
                assert not rig.call(
                    ["docker", "ps", "-aq", "--filter", "ancestor=" + ref]
                ).stdout.strip()
                rig.call(["docker", "image", "rm", ref])
                assert not image_present(rig, ref)
            result["cleanup"] = "PASS"
        except Exception as error:
            result["cleanup_error"] = str(error)
            result["status"] = "FAIL"
        result["seconds"] = time.monotonic() - started
        result["secret_files"] = sum(p.is_file() for p in (root / "secrets").rglob("*"))
        (root / "evidence/app-build.json").write_text(json.dumps(result, indent=2) + "\n")
    assert result["status"] == "PASS_COMPLETE_NORMAL_BUILD", result.get("error")


if __name__ == "__main__":
    main()
