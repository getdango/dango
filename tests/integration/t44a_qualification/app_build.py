"""tests/integration/t44a_qualification/app_build.py

Offline full install without lifecycles, then real early upstream compiler gates.
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
    # Reserve2GiB for these three compiler outputs/tsbuildinfo, plus the profile's
    # bounded64MiB tmpfs and8MiB log. Separate2GiB engineering headroom is added
    # by disk_gate, before install and again before EACH actual compiler command.
    return virtual * 2 + cache_bytes * 2 + 2 * GIB + (64 + 8) * 1024**2


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
    rig = runtime.Rig(root, {})
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
        rig.run("installer", ["-c", "node /scripts/app_build_smoke.cjs"], smoke=True)
        rig.smokes["installer"] = rig.signature("installer")
        result["running_build_profile_smoke"] = "PASS"
        pnpm = "/tools/app-pnpm/pnpm --dir /work/app/project"
        environment = "export PATH=/tools/app-pnpm:$PATH XDG_CACHE_HOME=/home/probe/cache XDG_CONFIG_HOME=/home/probe/config XDG_DATA_HOME=/home/probe/data PNPM_CONFIG_CACHE_DIR=/work/app/pnpm-cache; "
        disk_gate(rig, remaining, "before-offline-full-install-no-lifecycles")
        rig.run(
            "installer",
            [
                "-c",
                environment
                + pnpm
                + " install --offline --frozen-lockfile --ignore-scripts --store-dir /work/app/store --config.manage-package-manager-versions=false",
            ],
        )
        result["install"] = "PASS_OFFLINE_FULL_NO_LIFECYCLES"
        project = target / "project"
        assert critical(project, lock) == before
        for command in ["formula:build", "common-build", "warehouses-build"]:
            disk_gate(rig, remaining, "before-normal-" + command)
            rig.run("installer", ["-c", environment + pnpm + " run " + command])
            result["build"][command] = "PASS"
            assert critical(project, lock) == before
            assert {
                name: sha((project / name).read_bytes()) for name in source_before
            } == source_before
        disk_gate(rig, 0, "after-early-normal-TypeScript-builds")
        result["status"] = "PASS_EARLY_NORMAL_BUILD_GATE"
        result["lifecycles"] = (
            "ALL unexecuted; no canvas/Cypress/legacy DuckDB download or rebuild. This proves early compilation only; remaining runtime native needs remain gated."
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
    assert result["status"] == "PASS_EARLY_NORMAL_BUILD_GATE", result.get("error")


if __name__ == "__main__":
    main()
