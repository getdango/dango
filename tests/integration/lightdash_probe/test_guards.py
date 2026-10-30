"""tests/integration/lightdash_probe/test_guards.py

offline adverse gate checks.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from runtime import PINS, Rig


@pytest.fixture
def rig(tmp_path):
    for name in ["source", "tools", "scripts", "work", "home", "fixture", "secrets", "evidence"]:
        (tmp_path / name).mkdir()
    r = Rig(tmp_path, {"password": "SENTINEL-UNIQUE-RAW-SECRET"})
    r.images = {"node": {"Id": PINS["images"]["node"]["config"]}}
    return r


def test_missing_failed_and_stale_smoke_block_build(rig):
    with pytest.raises(AssertionError, match="MISSING/FAIL/STALE"):
        rig.require_smoke("installer")
    rig.smokes["installer"] = "FAIL"
    with pytest.raises(AssertionError, match="MISSING/FAIL/STALE"):
        rig.require_smoke("installer")
    rig.smokes["installer"] = rig.signature("installer")
    rig.require_smoke("installer")
    (rig.root / "source").chmod(0o700)
    with pytest.raises(AssertionError, match="MISSING/FAIL/STALE"):
        rig.require_smoke("installer")


def test_raw_leak_fails_and_serialized_export_is_sanitized(rig, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, "password=SENTINEL-UNIQUE-RAW-SECRET", ""
        ),
    )
    with pytest.raises(AssertionError, match="Raw subprocess secret exposure"):
        rig.call(["false"])
    exported = (rig.root / "evidence/commands.json").read_bytes()
    assert b"SENTINEL-UNIQUE-RAW-SECRET" not in exported
    assert json.loads(exported)[0]["raw_secret_leak"] is True


@pytest.mark.parametrize(
    "stderr",
    [
        "permission denied",
        "Cannot connect to the Docker daemon",
        "Error: No such object: another-name",
    ],
)
def test_inspect_errors_never_become_verified_absence(rig, monkeypatch, stderr):
    monkeypatch.setattr(
        rig, "call", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", stderr)
    )
    with pytest.raises(AssertionError):
        rig.remove("exact-owned-name")


@pytest.mark.parametrize("field", ["Id", "Architecture", "Layers"])
def test_image_binding_rejects_wrong_config_platform_or_layer_order(rig, monkeypatch, field):
    pin = PINS["images"]["node"]
    image = {
        "Id": pin["config"],
        "Architecture": "amd64",
        "Os": "linux",
        "RootFS": {"Layers": list(pin["diff_ids"])},
        "RepoDigests": [pin["ref"]],
    }
    if field == "Layers":
        image["RootFS"]["Layers"].reverse()
    else:
        image[field] = "wrong"
    monkeypatch.setattr(
        rig, "call", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, json.dumps([image]), "")
    )
    with pytest.raises(AssertionError):
        rig.inspect_image("node")


@pytest.mark.parametrize("remaining", [None, 14_000_000_000])
def test_unbounded_or_oversized_growth_refused(rig, monkeypatch, remaining):
    from types import SimpleNamespace

    import acquire

    monkeypatch.setattr(acquire, "bytes_used", lambda root: 0)
    monkeypatch.setattr(
        acquire.shutil, "disk_usage", lambda root: SimpleNamespace(free=100_000_000_000)
    )
    rig.images = {}
    with pytest.raises(AssertionError):
        acquire.disk_gate(rig, remaining, "adverse-storage-case")


def test_capture_failure_does_not_skip_exact_cleanup(rig, monkeypatch):
    rig.names = ["owned"]
    calls = []

    def fail(name):
        raise AssertionError("raw leak captured and sanitized")

    monkeypatch.setattr(rig, "capture", fail)
    monkeypatch.setattr(rig, "remove", lambda name: calls.append(name))
    with pytest.raises(AssertionError, match="Cleanup failures"):
        rig.cleanup()
    assert calls == ["owned"]


def test_host_population_handoff_with_different_uid_and_fresh_smoke(rig, monkeypatch):
    from types import SimpleNamespace

    import acquire

    path = rig.root / "source"
    original_stat = Path.stat
    owner = [501, 20]
    transitions = []

    def stat(current, *args, **kwargs):
        actual = original_stat(current, *args, **kwargs)
        if current == path:
            return SimpleNamespace(st_uid=owner[0], st_gid=owner[1], st_mode=actual.st_mode)
        return actual

    def chown(argv, **kwargs):
        assert argv[:3] == ["sudo", "chown", "-R"]
        owner[:] = [int(part) for part in argv[3].split(":")]
        transitions.append(tuple(owner))

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(acquire.os, "getuid", lambda: 1001)
    monkeypatch.setattr(acquire.os, "getgid", lambda: 121)
    monkeypatch.setattr(rig, "call", chown)
    rig.smokes["installer"] = rig.signature("installer")
    acquire.handoff(rig, ["source"], runtime=False)
    assert owner == [1001, 121] and not rig.smokes
    # The helper also performs an actual exclusive host-write/unlink probe.
    assert not list(path.glob(".host-population-*"))
    (path / "populated-public-input").write_text("public input")
    acquire.handoff(rig, ["source"], runtime=True)
    assert transitions == [(1001, 121), (501, 20)]
    assert (path.stat().st_mode & 0o777) == 0o755
    with pytest.raises(AssertionError, match="MISSING/FAIL/STALE"):
        rig.require_smoke("installer")


def test_failed_host_ownership_transfer_blocks_population(rig, monkeypatch):
    from types import SimpleNamespace

    import acquire

    path = rig.root / "source"
    original_stat = Path.stat

    def stat(current, *args, **kwargs):
        actual = original_stat(current, *args, **kwargs)
        if current == path:
            return SimpleNamespace(st_uid=501, st_gid=20, st_mode=actual.st_mode)
        return actual

    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(acquire.os, "getuid", lambda: 1001)
    monkeypatch.setattr(acquire.os, "getgid", lambda: 121)
    monkeypatch.setattr(rig, "call", lambda *args, **kwargs: None)
    with pytest.raises(AssertionError, match="ownership/mode handoff failed"):
        acquire.handoff(rig, ["source"], runtime=False)
    assert not list(path.iterdir())


def test_compressed_image_size_counterexample_refused_before_pull(rig, monkeypatch):
    from types import SimpleNamespace

    import acquire

    monkeypatch.setattr(acquire, "bytes_used", lambda root: 11_000_000_000)
    monkeypatch.setattr(
        acquire.shutil, "disk_usage", lambda root: SimpleNamespace(free=100_000_000_000)
    )
    rig.images = {}  # All planned images must count even before the first pull.
    assert 11_000_000_000 + 300_000_000 + acquire.HEADROOM + 472_134_264 < acquire.LIMIT
    with pytest.raises(AssertionError, match="14GB task disk bound failed"):
        acquire.disk_gate(rig, 300_000_000, "reviewer-compressed-size-counterexample")
    receipt = rig.receipts[-1]
    assert set(receipt["planned_image_bounds"]) == {"node", "postgres"}
    assert receipt["image_bound"] >= 230_363_511 + 435_032_886


def test_missing_frozen_expansion_bound_refused(rig, monkeypatch):
    import acquire

    node = dict(PINS["images"]["node"])
    node.pop("disk_bound")
    monkeypatch.setitem(PINS["images"], "node", node)
    with pytest.raises(AssertionError, match="Missing image expansion bound"):
        acquire.image_disk_bound()


@pytest.fixture
def ci_pg_namespace(rig, monkeypatch):
    # Public CI41050582181: PG create/inspect33, running inspect46, Node inspect39.
    rig.pg = "lightdash-r08-2389-postgres-1"
    rig.label = "lightdash-r08-2389"
    rig.pg_id = "5c39d3ecf8a5fe9236fc8f76af9ea853cf592772ee773666411a3fa911b717fa"
    rig.images["postgres"] = {"Id": PINS["images"]["postgres"]["config"]}
    pg = {
        "Id": rig.pg_id,
        "Name": "/" + rig.pg,
        "Image": PINS["images"]["postgres"]["config"],
        "Config": {
            "User": "999:999",
            "Image": PINS["images"]["postgres"]["ref"],
            "Labels": {"probe.owner": rig.label},
        },
        "State": {
            "Running": True,
            "Status": "running",
            "Paused": False,
            "Restarting": False,
            "Dead": False,
        },
        "HostConfig": {"NetworkMode": "none", "PortBindings": {}},
    }

    def inspect(argv, **kwargs):
        assert argv == ["docker", "inspect", "lightdash-r08-2389-postgres-1"]
        return subprocess.CompletedProcess(argv, 0, json.dumps([pg]), "")

    monkeypatch.setattr(rig, "call", inspect)
    return rig, pg, "container:" + rig.pg_id


def test_real_ci_canonical_pg_namespace_accepted(ci_pg_namespace):
    rig, pg, mode = ci_pg_namespace
    assert mode != "container:" + rig.pg
    rig.shared_network(mode)


@pytest.mark.parametrize(
    "defect",
    [
        "wrong-cid",
        "replacement-id",
        "other-owner",
        "other-image",
        "other-ref",
        "missing-id",
        "missing-created-id",
        "other-name",
        "stopped",
        "external-network",
    ],
)
def test_shared_namespace_rejects_unverified_pg(ci_pg_namespace, defect):
    rig, pg, mode = ci_pg_namespace
    if defect == "wrong-cid":
        mode = "container:" + "a" * 64
    elif defect == "replacement-id":
        pg["Id"] = "a" * 64
        mode = "container:" + pg["Id"]
    elif defect == "other-owner":
        pg["Config"]["Labels"]["probe.owner"] = "unrelated"
    elif defect == "other-image":
        pg["Image"] = "sha256:" + "a" * 64
    elif defect == "other-ref":
        pg["Config"]["Image"] = "postgres:latest"
    elif defect == "missing-id":
        del pg["Id"]
    elif defect == "missing-created-id":
        del rig.pg_id
    elif defect == "other-name":
        pg["Name"] = "/unrelated"
    elif defect == "stopped":
        pg["State"]["Running"] = False
    elif defect == "external-network":
        pg["HostConfig"]["NetworkMode"] = "bridge"
    with pytest.raises(AssertionError):
        rig.shared_network(mode)
