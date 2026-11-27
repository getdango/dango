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

LIMIT = 18_000_000_000
HEADROOM = 2 * GIB


def digest(data):
    """Return the artifact identity used in receipts."""
    return hashlib.sha256(data).hexdigest()


def fetch(url, sha256=None, integrity=None, maximum=256 * 1024**2):
    """Fetch public HTTPS bytes without credentials and enforce artifact bounds."""
    parsed = urllib.parse.urlparse(url)
    assert parsed.scheme == "https" and not parsed.username and not parsed.password
    assert parsed.hostname in {"registry.npmjs.org", "api.github.com", "extensions.duckdb.org"}
    headers = (
        {"User-Agent": "Dango-Lightdash-Compatibility-Probe/1.0"}
        if parsed.hostname == "extensions.duckdb.org"
        else {}
    )
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=120
    ) as response:
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
    assert owned + image_bound + remaining + HEADROOM < LIMIT, "18GB task disk bound failed"
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
    rig.source_bound = info["allocated_bound"]
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


def no_materialization(root):
    """Reject pnpm dependency state, while allowing ordinary source store dirs."""
    forbidden = {"node_modules", ".pnpm", ".modules.yaml", ".pnp.cjs"}
    found = [
        str(p.relative_to(root))
        for name in ["source", "work", "home"]
        for p in (root / name).rglob("*")
        if p.name in forbidden
    ]
    for relative in ["work/store", "home/data/pnpm/store", "home/.local/share/pnpm/store"]:
        path = root / relative
        if path.exists() or path.is_symlink():
            found.append(relative)
    assert not found, "Graph command materialized dependencies/store: " + repr(found)


def graph_packages(projects, lock):
    """Validate pinned pnpm list nodes, then count its already-resolved identities."""
    assert isinstance(projects, list) and projects
    roots = {p["name"]: p["path"] for p in projects if p.get("name")}
    assert "@lightdash/warehouses" in roots
    selected, expanded, deduplicated = set(), set(), set()

    def visit(node):
        assert isinstance(node, dict), "Invalid pnpm child shape"
        assert set(node) <= {
            "from",
            "version",
            "resolved",
            "path",
            "dependencies",
            "deduped",
            "dedupedDependenciesCount",
        }, "Unhandled pnpm child fields"
        assert all(isinstance(node.get(k), str) and node[k] for k in ["from", "version", "path"]), (
            "Missing pnpm child identity"
        )
        version = node["version"]
        identity = (node["from"], version)
        if version.startswith(("link:", "file:")):
            assert roots.get(node["from"]) == node["path"], "Unrecognized workspace node"
        else:
            key = node["from"] + "@" + version.split("(", 1)[0]
            assert key in lock["packages"], "Listed package absent from frozen lock: " + key
            selected.add(key)
        if node.get("deduped", False):
            assert node["deduped"] is True
            deduplicated.add(identity)
        else:
            expanded.add(identity)
        children = node.get("dependencies", {})
        assert isinstance(children, dict), "Invalid pnpm dependency edges"
        for child in children.values():
            visit(child)

    for project in projects:
        assert set(project) <= {
            "name",
            "version",
            "path",
            "private",
            "dependencies",
            "optionalDependencies",
        }, "Unhandled pnpm project fields"
        for field in ["dependencies", "optionalDependencies"]:
            edges = project.get(field, {})
            assert isinstance(edges, dict), "Invalid pnpm project edges"
            for child in edges.values():
                visit(child)
    assert deduplicated <= expanded, "Unexpanded deduplicated pnpm node"
    assert "@duckdb/node-api@2.0.0-r.1" in selected
    return sorted(selected)


def no_importer_links(project, lock):
    """Verify fetch created only virtual-store state, without project links/bins."""
    for importer in lock["importers"]:
        assert not Path(importer).is_absolute() and ".." not in Path(importer).parts
        modules = project / importer / "node_modules"
        if importer != ".":
            assert not modules.exists() and not modules.is_symlink(), (
                "Fetch linked a workspace importer"
            )
        elif modules.exists():
            for entry in modules.iterdir():
                assert entry.name == ".pnpm" or (
                    entry.name in {".modules.yaml", ".pnpm-workspace-state-v1.json"}
                    and entry.is_file()
                ), "Fetch linked a root importer/bin"
                assert not entry.is_symlink(), "Fetch created importer symlink"


class UniqueLockLoader(yaml.SafeLoader):
    """Safe PyYAML loader that refuses duplicate mapping keys."""

    def construct_mapping(self, node, deep=False):
        """Preserve SafeLoader construction after rejecting ambiguous keys."""
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        assert len(keys) == len(set(keys)), "Duplicate YAML mapping key"
        return super().construct_mapping(node, deep=deep)


def frozen_project_lock(text):
    """Read the exact tooling-document then project-document frozen lock layout."""
    docs = list(yaml.load_all(text, Loader=UniqueLockLoader))
    assert len(docs) == 2 and all(isinstance(d, dict) for d in docs), (
        "Expected tooling+project lock documents"
    )
    tooling, project = docs
    assert set(tooling) == {"lockfileVersion", "importers", "packages", "snapshots"}, (
        "Unhandled tooling lock schema"
    )
    assert tooling["lockfileVersion"] == project.get("lockfileVersion") == "9.0"
    assert set(tooling["importers"]) == {"."}, "Unexpected tooling importer"
    manager = tooling["importers"]["."]
    assert set(manager) == {"configDependencies", "packageManagerDependencies"}
    assert manager["configDependencies"] == {} and set(manager["packageManagerDependencies"]) == {
        "pnpm"
    }
    assert manager["packageManagerDependencies"]["pnpm"] == {
        "specifier": "12.3.4",
        "version": "12.3.4",
    }
    assert (
        set(tooling["packages"]) == set(tooling["snapshots"])
        and "pnpm@12.3.4" in tooling["packages"]
    )
    assert set(project) == {
        "lockfileVersion",
        "settings",
        "overrides",
        "packageExtensionsChecksum",
        "pnpmfileChecksum",
        "patchedDependencies",
        "importers",
        "packages",
        "snapshots",
    }, "Unhandled project lock schema"
    assert all(
        isinstance(project[k], dict)
        for k in [
            "settings",
            "overrides",
            "patchedDependencies",
            "importers",
            "packages",
            "snapshots",
        ]
    )
    assert {".", "packages/warehouses", "packages/common"} <= set(project["importers"]), (
        "Missing project importers"
    )
    assert "@duckdb/node-api@2.0.0-r.1" in project["packages"], "Missing frozen native package"
    return project


def packages(rig):
    """Standard production fetch, then actual cache/peak measurement before install."""
    handoff(rig, ["work"], runtime=False)
    original = frozen_project_lock((rig.root / "source/pnpm-lock.yaml").read_text())
    graph_bytes = (rig.root / "work/selected-graph.json").read_bytes()
    selected = graph_packages(json.loads(graph_bytes), original)
    project = rig.root / "work/project"
    disk_gate(rig, rig.source_bound, "before-public-source-copy-and-production-fetch")
    shutil.copytree(rig.root / "source", project, symlinks=True)
    # Explicit warehouse workspace path; existing consumer scripts stay unchanged.
    (rig.root / "work/deploy").symlink_to("project/packages/warehouses", target_is_directory=True)
    pnpm = str(rig.root / "tools/exe.linux-x64/pnpm")
    rig.call(
        [
            "env",
            "PNPM_CONFIG_CACHE_DIR=" + str(rig.root / "work/pnpm-cache"),
            pnpm,
            "--dir",
            str(project),
            "fetch",
            "--prod",
            "--config.ignore-scripts=true",
            "--store-dir",
            str(rig.root / "work/store"),
            "--config.manage-package-manager-versions=false",
        ],
        timeout=600,
    )
    # fetch is whole-workspace production acquisition, not the selected graph.
    # Count ALL actual fetched data/cache here, before any offline install/build.
    no_importer_links(project, original)
    for name, binding in PINS["source"].items():
        assert digest((project / name).read_bytes()) == binding["after"]
    disk_gate(rig, 0, "after-whole-production-fetch-before-install")
    cache = rig.root / "work/pnpm-cache"
    cache_bytes = bytes_used(cache) if cache.exists() else 0
    fetched_owned = bytes_used(rig.root)
    rows, total, archive_bytes = [], 0, 0
    for key in selected:
        resolution = original["packages"][key]["resolution"]
        name, version = key.rsplit("@", 1)
        assert "(" not in version and ":" not in version
        url = (
            resolution.get("tarball")
            or f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz"
        )
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
    # Existing measured fetch data is in owned bytes already. Count four further
    # unshared selected-install copies, two cache rewrite copies, and 2GiB for
    # compiler/fixture/snapshots/extensions/logs. Offline forbids new metadata GETs.
    rig.install_remaining = total * 4 + cache_bytes * 2 + 2 * GIB
    disk_gate(rig, rig.install_remaining, "actual-fetch-cache-plus-remaining-offline-install-peak")
    (rig.root / "evidence/package-inventory.json").write_text(
        json.dumps(
            {
                "packages": rows,
                "graph_sha256": digest(graph_bytes),
                "graph_command_no_materialization": True,
                "acquisition_scope": "whole-workspace production pnpm fetch; no importer links/lifecycles",
                "actual_fetched_owned_bytes": fetched_owned,
                "actual_metadata_cache_bytes": cache_bytes,
                "remaining_cache_rewrite_bound": cache_bytes * 2,
                "selected_compressed_total": archive_bytes,
                "selected_allocated_bound": total,
                "remaining_offline_install_bound": rig.install_remaining,
            },
            indent=2,
        )
        + "\n"
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
