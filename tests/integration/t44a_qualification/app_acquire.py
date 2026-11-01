"""tests/integration/t44a_qualification/app_acquire.py

Acquisition only: standard frozen full-development fetch, never install/build.
"""

import base64
import collections
import concurrent.futures
import hashlib
import importlib
import io
import json
import math
import platform
import sys
import tarfile
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from run import PINS, disk_gate, sha

HERE = Path(__file__).resolve().parent
HOOK = "a3542d5889601238cbf1b0d246bf674301bfa0cd96b6d9f040d1c97afa7f20c8"
ACQUIRE = "0037b315c723206020e7ec9a5fb5ad0e3bdcda24105a13daf30ab7bc4b0b1273"
YAML_SHA = "ba1cc08a7ccde2d2ec775841541641e4548226580ab850948cbfda66a1befcdc"
YAML_URL = (
    "https://files.pythonhosted.org/packages/8b/9d/"
    "b3589d3877982d4f2329302ef98a8026e7f4443c765c46cfecc8858c6b4b/"
    "pyyaml-6.0.3-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64."
    "manylinux_2_28_x86_64.whl"
)


def public_bytes(url, integrity):
    """Read one bounded public integrity-bound artifact without credentials."""
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https" and not parsed.username and not parsed.password
    assert parsed.hostname in {"registry.npmjs.org", "nodejs.org", "files.pythonhosted.org"}
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(256 * 1024**2 + 1)
    assert len(data) <= 256 * 1024**2, "Archive inspection memory bound"
    algorithm, expected = integrity.split("-", 1)
    assert algorithm in {"sha256", "sha512"}
    assert base64.b64encode(hashlib.new(algorithm, data).digest()).decode() == expected
    return data


def applicable(values, target):
    """Honor explicit lock platform exclusions; this does not resolve dependencies."""
    assert isinstance(values, list) and all(isinstance(v, str) for v in values)
    positives = [v for v in values if not v.startswith("!")]
    return "!" + target not in values and (not positives or target in positives or "any" in values)


def artifact_rows(lock):
    """Enumerate frozen records for Linux/x64/glibc, without graph resolution."""
    for key, package in sorted(lock["packages"].items()):
        if not all(
            applicable(package.get(field, []), target)
            for field, target in [("os", "linux"), ("cpu", "x64"), ("libc", "glibc")]
        ):
            continue
        resolution = package["resolution"]
        if resolution.get("type") == "variations":
            assert key == "node@runtime:24.22.1", "Unhandled frozen platform-variant record"
            matches = [
                v["resolution"]
                for v in resolution["variants"]
                if v["targets"] == [{"cpu": "x64", "os": "linux"}]
            ]
            assert len(matches) == 1
            yield key, matches[0]["url"], matches[0]["integrity"]
        else:
            name, version = key.rsplit("@", 1)
            assert ":" not in version and "(" not in version
            assert set(resolution) <= {"integrity", "tarball"} and resolution.get("integrity")
            url = resolution.get("tarball") or (
                f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz"
            )
            yield key, url, resolution["integrity"]


def inspect_archive(item, inventory):
    """Retain notices and native identities from original verified package bytes."""
    key, url, integrity = item
    data = public_bytes(url, integrity)
    info = inventory(data)
    row = {
        "package": key,
        "url": url,
        "integrity": integrity,
        "sha256": sha(data),
        "compressed": len(data),
        **info,
        "notices": {},
        "native": {},
    }
    notices = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = Path(member.name).name.lower()
            if name == "package.json" and member.name.count("/") == 1:
                metadata = json.load(archive.extractfile(member))
                row["license"] = metadata.get("license", metadata.get("licenses"))
                row["lifecycle"] = {
                    k: v
                    for k, v in metadata.get("scripts", {}).items()
                    if k in {"preinstall", "install", "postinstall"}
                }
            if any(
                name.startswith(prefix) for prefix in ["license", "licence", "notice", "copyright"]
            ):
                assert member.size <= 256 * 1024**2, "Notice inspection memory bound"
                body = archive.extractfile(member).read()
                identity = sha(body)
                notices[identity] = body
                row["notices"][member.name] = {"sha256": identity, "bytes": len(body)}
            magic = archive.extractfile(member).read(16)
            if magic.startswith(b"\x7fELF") or name.endswith((".node", ".so")):
                assert member.size <= 256 * 1024**2, "Native inspection memory bound"
                body = archive.extractfile(member).read()
                row["native"][member.name] = {
                    "sha256": sha(body),
                    "bytes": len(body),
                    "magic_hex": magic.hex(),
                    "elf": magic.startswith(b"\x7fELF"),
                }
    return row, notices


def fetch_peak(rows, lock):
    """Count expanded virtual-store peer copies without assuming hardlink sharing."""
    snapshots = collections.Counter(key.split("(", 1)[0] for key in lock["snapshots"])
    # One content-store copy, every peer-context virtual-store copy, and one
    # full extraction/staging copy. inventory() also includes a 4KiB metadata
    # allowance per archive member per copy. Count two compressed cache/temp
    # copies separately; no compressed=expanded or deduplication credit.
    return sum(
        row["allocated_bound"] * (2 + max(1, snapshots[row["package"]])) + 2 * row["compressed"]
        for row in rows
    )


def critical(project, lock):
    """Bind package manifests, workspace, hook and adapted lock before/after fetch."""
    paths = ["pnpm-lock.yaml", "pnpm-workspace.yaml", ".pnpmfile.cjs"]
    paths += [str(Path(p) / "package.json") for p in lock["importers"]]
    return {p: sha((project / p).read_bytes()) for p in paths}


def main():
    """Follow the proven diagnostic with bounded public acquisition, then stop."""
    root, baseline = [Path(p).resolve() for p in sys.argv[1:]]
    assert platform.system() == "Linux" and platform.machine() == "x86_64"
    assert json.loads((root / "evidence/result.json").read_text())["status"] == (
        "PASS_DBT_DRIVER_DIAGNOSTIC"
    ), "Prior real native diagnostic must pass"
    assert not any(p.is_file() for p in (root / "secrets").rglob("*")), "Prior secret cleanup"
    base = baseline / "tests/integration/lightdash_probe"
    assert sha((base / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    assert sha((base / "acquire.py").read_bytes()) == ACQUIRE
    sys.path.insert(0, str(base))
    runtime = importlib.import_module("runtime")
    rig = runtime.Rig(root, {})
    rig.receipts = json.loads((root / "evidence/commands.json").read_text())
    rig.docker_root = Path(
        json.loads(rig.call(["docker", "info", "--format", "{{json .DockerRootDir}}"]).stdout)
    )
    app = root / "app-acquisition"
    assert not app.exists(), "Acquisition subtree must be fresh"
    app.mkdir()
    started = time.monotonic()
    result = {"status": "FAIL", "install": "UNRUN", "build": "UNRUN", "full_app": "UNRUN"}
    rows = []
    try:
        # Reuse the exact R08 parser wheel as a zip-imported pure Python tool;
        # no pip installation, native extension execution or global cache.
        disk_gate(rig, 1024**3, "before-small-source-and-acquisition-tool-bootstrap")
        wheel = app / "pyyaml.whl"
        wheel.write_bytes(
            public_bytes(YAML_URL, "sha256-" + base64.b64encode(bytes.fromhex(YAML_SHA)).decode())
        )
        with zipfile.ZipFile(wheel) as archive:
            assert any(n.endswith("licenses/LICENSE") for n in archive.namelist())
        sys.path.insert(0, str(wheel))
        acquire = importlib.import_module("acquire")
        assert str(wheel) in sys.modules["yaml"].__file__, "Verified parser tool required"
        assert acquire.PINS["images"] == PINS["images"]
        source_pin = acquire.PINS["lightdash"]
        data = acquire.fetch(source_pin["archive_url"], source_pin["archive_sha256"])
        source_size = acquire.inventory(data)["allocated_bound"]
        disk_gate(rig, source_size + len(data), "before-complete-source-extraction")
        project = app / "project"
        acquire.unpack(data, project)
        rig.call(["patch", "-d", str(project), "-p1", "-i", str(base / "lightdash.patch")])
        for name, pin in acquire.PINS["source"].items():
            assert sha((project / name).read_bytes()) == pin["after"]
        assert sha((project / ".pnpmfile.cjs").read_bytes()) == HOOK
        lock = acquire.frozen_project_lock((project / "pnpm-lock.yaml").read_text())
        before = critical(project, lock)
        pin = acquire.PINS["tools"]["exe.linux-x64"]
        data = acquire.fetch(pin["url"], pin["sha256"])
        disk_gate(rig, acquire.inventory(data)["allocated_bound"], "before-pnpm-tool-extraction")
        acquire.unpack(data, app / "pnpm")
        pnpm = str(app / "pnpm/pnpm")
        assert rig.call([pnpm, "--version"]).stdout.strip() == "12.3.4"
        notices = {}
        items = list(artifact_rows(lock))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            for row, retained in pool.map(
                lambda item: inspect_archive(item, acquire.inventory), items
            ):
                rows.append(row)
                notices.update(retained)
        payload = {
            "scope": "Whole Linux/x64/glibc development lock inventory, not selected closure",
            "packages": rows,
            "missing_declared_license": [
                r["package"] for r in rows if not r.get("license") and not r["notices"]
            ],
            "acquisition_only": True,
        }
        evidence = json.dumps(payload, indent=2).encode()
        assert len(evidence) <= 32 * 1024**2
        disk_gate(rig, len(evidence) * 2, "before-small-inventory-evidence")
        (root / "evidence/app-package-inventory.json").write_bytes(evidence + b"\n")
        remaining = fetch_peak(rows, lock)
        remaining += sum(math.ceil(len(b) / 4096) * 4096 + 4096 for b in notices.values())
        remaining += len(evidence) * 2
        result["before_fetch_remaining_upper"] = remaining
        disk_gate(rig, remaining, "before-full-development-fetch-expanded-virtual-store-peak")
        license_dir = app / "notices"
        license_dir.mkdir()
        for identity, body in notices.items():
            (license_dir / identity).write_bytes(body)
        cache, store = app / "pnpm-cache", app / "store"
        argv = [
            "env",
            "PNPM_CONFIG_CACHE_DIR=" + str(cache),
            pnpm,
            "--dir",
            str(project),
            "fetch",
            "--config.ignore-scripts=true",
            "--store-dir",
            str(store),
            "--config.manage-package-manager-versions=false",
        ]
        rig.call(argv, timeout=1200)
        acquire.no_importer_links(project, lock)
        assert critical(project, lock) == before, "Frozen source inputs changed"
        disk_gate(rig, 0, "after-full-development-fetch-no-install")
        observed = {}
        for name, path in [
            ("whole_task", root),
            ("content_store", store),
            ("virtual_store", project / "node_modules/.pnpm"),
            ("cache", cache),
        ]:
            observed[name] = acquire.bytes_used(path) if path.exists() else 0
        result["observed_allocated"] = observed
        known_native = collections.defaultdict(list)
        for row in rows:
            for member, identity in row["native"].items():
                known_native[identity["sha256"]].append(
                    {"package": row["package"], "member": member, "archive_sha256": row["sha256"]}
                )
        observed_native = {}
        for suffix in ["*.node", "*.so"]:
            for path in project.rglob(suffix):
                if path.is_file():
                    body = path.read_bytes()
                    identity = sha(body)
                    observed_native[str(path.relative_to(project))] = {
                        "sha256": identity,
                        "bytes": len(body),
                        "magic_hex": body[:16].hex(),
                        "elf": body.startswith(b"\x7fELF"),
                        "archive_members": known_native.get(identity, []),
                    }
        result["observed_native"] = observed_native
        result["uninventoried_native"] = [
            path for path, identity in observed_native.items() if not identity["archive_members"]
        ]
        # Persist sizes and every inspected path/hash/magic BEFORE the refusal.
        (root / "evidence/app-acquisition.json").write_text(json.dumps(result, indent=2) + "\n")
        assert not result["uninventoried_native"], "Uninventoried native output: " + repr(
            result["uninventoried_native"]
        )
        result.update(
            status="PASS_FULL_DEV_ACQUISITION_ONLY",
            observed_allocated=observed,
            observed_native=observed_native,
            hook_sha256=HOOK,
            inventory_sha256=sha(evidence + b"\n"),
            packages=len(rows),
            missing_license_count=len(payload["missing_declared_license"]),
            next_install_gate="BLOCKED pending license gaps/lifecycle bytes and full install/build peak",
        )
    except Exception as error:
        result["error"] = str(error)
    finally:
        result["seconds"] = time.monotonic() - started
        result["retained_task_owned_acquisition"] = str(app.relative_to(root))
        result["new_services_or_containers"] = 0
        result["secret_files"] = sum(p.is_file() for p in (root / "secrets").rglob("*"))
        (root / "evidence/app-acquisition.json").write_text(json.dumps(result, indent=2) + "\n")
    assert result["status"] == "PASS_FULL_DEV_ACQUISITION_ONLY", result.get("error")


if __name__ == "__main__":
    main()
