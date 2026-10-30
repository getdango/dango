"""tests/integration/lightdash_probe/acquire.py

public acquisition and disk gates.
"""

import base64
import gzip
import hashlib
import io
import json
import math
import os
import posixpath
import shutil
import subprocess
import tarfile
import urllib.parse
import urllib.request
from pathlib import Path

import yaml
from runtime import GIB, HERE, PINS

LIMIT = 14_000_000_000
HEADROOM = 2 * GIB


def digest(data):
    """Return the artifact identity used in receipts."""
    return hashlib.sha256(data).hexdigest()


def fetch(url, sha256=None, integrity=None, maximum=256 * 1024**2):
    """Fetch public HTTPS bytes without credentials and enforce artifact bounds."""
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https" and not parsed.username and not parsed.password
    assert parsed.hostname in {"registry.npmjs.org", "api.github.com", "extensions.duckdb.org"}
    with urllib.request.urlopen(url, timeout=120) as response:
        data = response.read(maximum + 1)
    assert len(data) <= maximum, "Artifact exceeds predeclared acquisition reserve"
    if sha256:
        assert digest(data) == sha256, "Frozen artifact hash mismatch"
    if integrity:
        algorithm, expected = integrity.split("-", 1)
        assert algorithm == "sha512"
        assert base64.b64encode(hashlib.sha512(data).digest()).decode() == expected
    return data


def inventory(data):
    """Measure an archive's allocated-file upper bound without extracting it."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        for item in members:
            assert not Path(item.name).is_absolute() and ".." not in Path(item.name).parts
            assert item.isfile() or item.isdir() or item.issym() or item.islnk()
            if item.issym() or item.islnk():
                assert not Path(item.linkname).is_absolute()
                target = posixpath.normpath(
                    posixpath.join(
                        posixpath.dirname(item.name) if item.issym() else "", item.linkname
                    )
                )
                prefix = item.name.split("/")[0]
                assert target == prefix or target.startswith(prefix + "/"), (
                    "Archive link escapes root"
                )
        return {
            "files": len(members),
            "logical": sum(m.size for m in members),
            "allocated_bound": sum(math.ceil(m.size / 4096) * 4096 + 4096 for m in members),
        }


def unpack(data, destination, strip=True):
    """Extract verified public content with the standard safe data filter."""
    inventory(data)
    destination = Path(destination)
    destination.mkdir(exist_ok=True, parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if strip:
            for member in members:
                member.name = "/".join(Path(member.name).parts[1:])
            members = [m for m in members if m.name]
        archive.extractall(destination, members=members, filter="data")


def bytes_used(root):
    """Count allocated owned files including independent materialized copies."""
    output = subprocess.check_output(["sudo", "du", "-s", "--block-size=1", str(root)], text=True)
    return int(output.split()[0])


def handoff(rig, names, runtime):
    """Transfer only owned population paths and invalidate prior readiness."""
    assert set(names) <= {"source", "tools", "scripts", "work", "home", "fixture"}
    rig.smokes.clear()
    uid, gid = (501, 20) if runtime else (os.getuid(), os.getgid())
    for name in names:
        path = rig.root / name
        assert path.is_dir() and not path.is_symlink()
        rig.call(["sudo", "chown", "-R", f"{uid}:{gid}", str(path)])
        state = path.stat()
        assert (state.st_uid, state.st_gid, state.st_mode & 0o777) == (uid, gid, 0o755), (
            "Population ownership/mode handoff failed"
        )
        if not runtime:
            probe = path / f".host-population-{os.getpid()}"
            with probe.open("x") as output:
                output.write("host-write-ok\n")
            probe.unlink()


def image_disk_bound():
    """Count all planned frozen images independently of Docker Size semantics."""
    assert set(PINS["images"]) == {"node", "postgres"}, "Missing planned image bounds"
    rows = {}
    for role, pin in PINS["images"].items():
        bound = pin.get("disk_bound", {})
        assert all(
            isinstance(bound.get(k), int) and bound[k] > 0
            for k in ["compressed", "layer_logical", "allocated_layer_upper"]
        ), "Missing image expansion bound"
        manifest_bytes = (HERE / "image-metadata" / f"{role}-manifest.json").read_bytes()
        assert "sha256:" + digest(manifest_bytes) == pin["ref"].split("@")[1]
        manifest = json.loads(manifest_bytes)
        assert bound["compressed"] == sum(layer["size"] for layer in manifest["layers"])
        assert bound["allocated_layer_upper"] >= bound["layer_logical"]
        # One compressed content cache, unpacked immutable layers, a full
        # decompression staging copy, and one full owned-container snapshot.
        # No dedup/overlay sharing credit; no image builds or extra containers.
        rows[role] = {**bound, "allocated_copies": 3, "metadata_reserve": 64 * 1024**2}
        rows[role]["total"] = (
            bound["compressed"] + 3 * bound["allocated_layer_upper"] + 64 * 1024**2
        )
    return sum(row["total"] for row in rows.values()), rows


def disk_gate(rig, remaining, reason):
    """Refuse unbounded growth against actual free space and the task budget."""
    assert isinstance(remaining, int) and remaining >= 0
    owned = bytes_used(rig.root)
    image_bound, image_details = image_disk_bound()
    free = min(shutil.disk_usage(p).free for p in [rig.root, Path("/var/lib/docker")])
    receipt = {
        "gate": reason,
        "owned_allocated": owned,
        "image_bound": image_bound,
        "planned_image_bounds": image_details,
        "remaining_upper_bound": remaining,
        "engineering_headroom": HEADROOM,
        "df_free_minimum": free,
        "task_limit": LIMIT,
    }
    rig.receipts.append(receipt)
    rig.save()
    assert owned + image_bound + remaining + HEADROOM < LIMIT, "14GB task disk bound failed"
    assert remaining + image_bound + HEADROOM < free, "Actual runner free-space gate failed"


def tools(rig):
    """Acquire only the pinned standard executables needed for readiness/build."""
    disk_gate(rig, 512 * 1024**2, "tiny-tools-before-readiness")
    for name, pin in PINS["tools"].items():
        data = fetch(pin["url"], pin["sha256"])
        unpack(data, rig.root / "tools" / name)
    rig.call(["chmod", "-R", "a+rX", str(rig.root / "tools")])


def source(rig):
    """Acquire the public frozen source and apply exactly the reviewed two-file diff."""
    disk_gate(rig, 2 * GIB, "before-public-source")
    handoff(rig, ["source"], runtime=False)
    pin = PINS["lightdash"]
    data = fetch(pin["archive_url"], pin["archive_sha256"])
    info = inventory(data)
    disk_gate(rig, info["allocated_bound"] + len(data), "before-source-extraction")
    unpack(data, rig.root / "source")
    for name, binding in PINS["source"].items():
        assert digest((rig.root / "source" / name).read_bytes()) == binding["before"]
    # patch cwd is explicit; no shell interpolation or credentials are involved.
    rig.call(["patch", "-d", str(rig.root / "source"), "-p1", "-i", str(HERE / "lightdash.patch")])
    for name, binding in PINS["source"].items():
        assert digest((rig.root / "source" / name).read_bytes()) == binding["after"]
    assert (rig.root / "source/LICENSE").is_file()
    handoff(rig, ["source"], runtime=True)


def packages(rig):
    """Inventory pnpm's dedicated lock; fetch with standard pnpm, never a resolver."""
    handoff(rig, ["work"], runtime=False)
    original = yaml.safe_load((rig.root / "source/pnpm-lock.yaml").read_text())
    deployed = yaml.safe_load((rig.root / "work/deploy/pnpm-lock.yaml").read_text())
    rows = []
    total = 0
    archive_bytes = 0
    for key, record in deployed["packages"].items():
        resolution = record["resolution"]
        if "directory" in resolution or key.startswith("file:"):
            continue  # Unchanged workspace source is already counted separately.
        assert key in original["packages"]
        assert resolution == original["packages"][key]["resolution"], (
            "Deployment changed frozen resolution"
        )
        name, version = key.rsplit("@", 1)
        assert "(" not in version and ":" not in version
        url = (
            resolution.get("tarball")
            or f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz"
        )
        disk_gate(rig, total * 8 + 2 * GIB + 512 * 1024**2, "before-next-package-inventory")
        data = fetch(url, integrity=resolution["integrity"])
        info = inventory(data)
        archive_bytes += len(data)
        total += info["allocated_bound"]
        rows.append(
            {
                "package": key,
                "sha256": digest(data),
                "integrity": resolution["integrity"],
                "compressed": len(data),
                **info,
            }
        )
    # Explicit peak: store + virtual store + unshared installation + metadata/temp,
    # counted as eight independent allocated copies. Reserve 2GiB for fixture,
    # compiler, dumps, logs, export and extension expansion; no dedup assumed.
    peak = total * 8 + 2 * GIB + 512 * 1024**2
    disk_gate(rig, peak, "before-standard-fetch-and-offline-install")
    (rig.root / "evidence/package-inventory.json").write_text(
        json.dumps(
            {
                "packages": rows,
                "compressed_total": archive_bytes,
                "allocated_bound": total,
                "peak_remaining": peak,
            },
            indent=2,
        )
        + "\n"
    )
    rig.install_remaining = total * 4 + 2 * GIB + 512 * 1024**2
    (rig.root / "work/store").mkdir(exist_ok=True)
    pnpm = str(rig.root / "tools/exe.linux-x64/pnpm")
    rig.call(
        [
            pnpm,
            "--dir",
            str(rig.root / "work/deploy"),
            "fetch",
            "--prod",
            "--ignore-scripts",
            "--store-dir",
            str(rig.root / "work/store"),
            "--config.manage-package-manager-versions=false",
        ],
        timeout=600,
    )


def extensions(rig):
    """Install exact signed extension bytes in the offline native search directory."""
    handoff(rig, ["home"], runtime=False)
    target = rig.root / "home/.duckdb/extensions/v2.0.0/linux_amd64"
    target.mkdir(parents=True)
    for name, pin in PINS["extensions"].items():
        data = gzip.decompress(fetch(pin["url"], pin["sha256"]))
        assert digest(data) == pin["expanded_sha256"]
        (target / (name + ".duckdb_extension")).write_bytes(data)
    handoff(rig, ["home"], runtime=True)
