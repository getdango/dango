"""tests/integration/t44a_qualification/run.py

Thin first-diagnostic recipe using the unchanged, pinned R08 Docker runtime.
"""

import gzip
import hashlib
import importlib
import json
import os
import secrets
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

from dbt_assets import EXTRA_PEAK, export_candidates
from dbt_publication import EXTRA_PEAK as PUBLICATION_PEAK
from dbt_publication import export_candidates as publication_exports
from dlt_source import artifacts as dlt_artifacts
from dlt_source import install_command as dlt_install_command
from dlt_source import remaining as dlt_remaining
from held_reader import PREREQUISITE, artifact_union, prerequisite
from native_namespaces import EXTRA_PEAK as NAMESPACES_PEAK

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text())
MIB = 1024**2
DLT_FILES = [
    "dlt_source.py",
    "producer_capture.py",
    "dlt-source-pins.json",
    "requirements-dlt.txt",
    "requirements-dlt-source.txt",
]


def sha(data):
    """Identify exact acquisition and script bytes."""
    return hashlib.sha256(data).hexdigest()


def disk_gate(rig, remaining, reason):
    """Account for all planned image copies, owned data, remaining peak and df."""
    assert isinstance(remaining, int) and remaining >= 0
    owned = int(rig.call(["sudo", "du", "-s", "--block-size=1", str(rig.root)]).stdout.split()[0])
    images = sum(
        p["disk_bound"]["compressed"] + 3 * p["disk_bound"]["allocated_layer_upper"] + 64 * MIB
        for p in PINS["images"].values()
    )
    free = min(shutil.disk_usage(p).free for p in [rig.root, rig.docker_root])
    headroom = PINS["limits"]["headroom_bytes"]
    row = {
        "gate": reason,
        "owned": owned,
        "all_planned_image_bound": images,
        "remaining": remaining,
        "headroom": headroom,
        "actual_df_min": free,
    }
    rig.receipts.append(row)
    rig.save()
    assert owned + images + remaining + headroom <= PINS["limits"]["task_bytes"], "30GB task bound"
    assert images + remaining + headroom <= free, "Actual runner df bound"


def download(url, expected, size):
    """Download bounded public bytes without tokens and require their frozen hash."""
    request = urllib.request.Request(url, headers={"User-Agent": "Dango-T44A-Qualification/1.0"})
    assert url.startswith("https://") and "@" not in urllib.parse.urlparse(url).netloc
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read(size + 1)
    assert len(data) == size and sha(data) == expected, "Frozen artifact identity/size mismatch"
    return data


def stage(rig):
    """Populate only the exact small diagnostic artifacts; no full app acquisition."""
    artifacts = rig.root / "tools/artifacts"
    artifacts.mkdir()
    for pin in rig.artifact_pins:
        disk_gate(rig, rig.remaining, "before-artifact-" + pin["file"])
        path = artifacts / pin["file"]
        path.write_bytes(download(pin["url"], pin["sha256"], pin["compressed"]))
        if pin["kind"] in {"python", "dbc"}:
            target = rig.root / "tools" if pin["kind"] == "python" else rig.root / "tools/dbc-tool"
            target.mkdir(exist_ok=True)
            with tarfile.open(path) as archive:
                archive.extractall(target, filter="data")
    target = rig.root / "home/.duckdb/extensions/v2.0.0/linux_amd64"
    target.mkdir(parents=True)
    for name, pin in PINS["extensions"].items():
        data = download(pin["url"], pin["sha256"], pin["compressed"])
        expanded = gzip.decompress(data)
        assert sha(expanded) == pin["expanded_sha256"]
        assert len(expanded) <= pin["expanded_upper"]
        (target / (name + ".duckdb_extension")).write_bytes(expanded)
    for name in ["source", "tools", "scripts", "work", "home", "fixture"]:
        rig.call(["sudo", "chown", "-R", "501:20", str(rig.root / name)])
    rig.call(["sudo", "chown", "-R", "999:999", str(rig.root / "pgdata")])
    disk_gate(rig, rig.remaining, "before-offline-tiny-venv-install")


def image_present(rig, ref):
    """Distinguish canonical missing images from daemon/permission failures."""
    result = rig.call(["docker", "image", "inspect", ref], check=False)
    if result.returncode == 0:
        return True
    assert "No such image:" in result.stderr, "Unknown image inspection failure"
    return False


def export(rig):
    """Allowlist genuine dbt outputs only after raw synthetic-secret checks."""
    candidates = [
        (rig.root / "work/diagnostic.json", "diagnostic.json"),
        (rig.root / "work/dbt-view.json", "dbt-view.json"),
        (rig.root / "work/native-namespaces.json", "native-namespaces.json"),
    ]
    candidates += [
        (rig.root / "work/project/target" / n, n)
        for n in ["manifest.json", "run_results.json", "catalog.json"]
    ]
    candidates += [
        (rig.root / "work/dbt-view-project/target" / n, "view-" + n)
        for n in ["manifest.json", "run_results.json", "catalog.json"]
    ]
    candidates += export_candidates(rig.root / "work")
    candidates += publication_exports(rig.root / "work")
    candidates += [
        (rig.root / "work" / name, name)
        for name in [
            "dlt-install.json",
            "producer-capture.json",
            "producer-first-load.json",
            "producer-first-state.json",
            "producer-second-load.json",
            "producer-second-state.json",
        ]
    ]
    for source, name in candidates:
        if source.exists():
            data = source.read_bytes()
            assert len(data) <= 32 * MIB, "Diagnostic output bound exceeded"
            assert not any(s.encode() in data for s in rig.secrets.values()), (
                "Artifact secret exposure"
            )
            if source.suffix == ".parquet":
                checked = json.loads((rig.root / "work/dbt-assets.json").read_text())
                assert (
                    sha(data) == checked["safe_parquet"][str(source.relative_to(rig.root / "work"))]
                ), "Missing/stale logical Parquet secret check"
            (rig.root / "evidence" / name).write_bytes(data)


def main():
    """Run one standard isolated dbt diagnostic and capture before owned cleanup."""
    assert len(sys.argv) in {3, 4}
    producer_capture = len(sys.argv) == 4
    assert not producer_capture or sys.argv[3] in {"--producer-capture", "--held-reader"}
    held_reader = producer_capture and sys.argv[3] == "--held-reader"
    expected_status = (
        PREREQUISITE
        if held_reader
        else "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE"
        if producer_capture
        else "PASS_DBT_DRIVER_DIAGNOSTIC"
    )
    root, baseline = [Path(p).resolve() for p in sys.argv[1:3]]
    assert not root.exists(), "Task root must be fresh"
    code = baseline / "tests/integration/lightdash_probe"
    assert sha((code / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    sys.path.insert(0, str(code))
    runtime = importlib.import_module("runtime")
    assert runtime.PINS["images"] == PINS["images"]
    root.mkdir(mode=0o755)
    for name in [
        "source",
        "tools",
        "scripts",
        "work",
        "home",
        "fixture",
        "secrets",
        "pgdata",
        "evidence",
    ]:
        (root / name).mkdir(mode=0o755)
    admin = secrets.token_hex(24)
    print("::add-mask::" + admin, flush=True)
    rig = runtime.Rig(root, {"adminPassword": admin})
    rig.producer_capture = producer_capture
    rig.artifact_pins = (
        artifact_union(PINS, dlt_artifacts(PINS))
        if held_reader
        else dlt_artifacts(PINS)
        if producer_capture
        else PINS["artifacts"]
    )
    rig.label = "t44a03-" + str(os.getpid())
    result = {
        "status": "FAIL",
        "native": "UNRUN",
        "full_app": "UNRUN",
        "head": os.environ["PROBE_HEAD"],
        "started_utc": time.strftime("%FT%TZ", time.gmtime()),
        "qualification_inputs": {
            name: sha((HERE / name).read_bytes())
            for name in [
                "diagnostic.py",
                "dbt_view.py",
                "dbt_assets.py",
                "dbt_publication.py",
                "native_namespaces.py",
                "run.py",
                "held_reader.py",
                "pins.json",
            ]
            + DLT_FILES
        },
    }
    existing = {}
    try:
        rig.docker_root = Path(
            json.loads(rig.call(["docker", "info", "--format", "{{json .DockerRootDir}}"]).stdout)
        )
        assert rig.docker_root.is_absolute()
        # Frozen expansion is counted conservatively even when caches/files overlap.
        # Additional planned peaks:512MiB configured DuckDB spill,256MiB tiny
        # PG data + four128MiB WAL segments,96MiB three allowlisted JSONs,
        # two8MiB logs, plus258MiB for the view: two sets of3x32MiB artifacts,
        # two32MiB report copies and2MiB source/filesystem overhead.
        # These are separate from2GiB headroom.
        rig.remaining = sum(p["compressed"] + 3 * p["expanded_upper"] for p in PINS["artifacts"])
        rig.remaining += sum(
            p["compressed"] + 2 * p["expanded_upper"] for p in PINS["extensions"].values()
        )
        rig.remaining += (512 + 256 + 4 * 128 + 96 + 16 + 258) * MIB
        rig.remaining += (
            EXTRA_PEAK  # Both original/exported assets targets, reports, logs and data.
        )
        rig.remaining += PUBLICATION_PEAK
        rig.remaining += NAMESPACES_PEAK
        if producer_capture:
            rig.remaining = dlt_remaining(PINS)
            if held_reader:
                rig.remaining += sum(
                    p["compressed"] + 3 * p["expanded_upper"] for p in PINS["artifacts"]
                )
        disk_gate(rig, rig.remaining, "before-public-acquisition-and-image-pulls")
        for role, pin in PINS["images"].items():
            existing[role] = image_present(rig, pin["ref"])
            rig.inspect_image(role)
        for name in ["source", "tools", "scripts", "secrets", "fixture"]:
            (root / name / "readiness").write_text("populated\n")
        (root / "fixture/data").mkdir()
        (root / "fixture/data/readiness").write_text("populated\n")
        for name in [
            "smoke.py",
            "diagnostic.py",
            "dbt_view.py",
            "dbt_assets.py",
            "dbt_publication.py",
            "native_namespaces.py",
            "pins.json",
            "requirements.txt",
            "held_reader.py",
        ] + DLT_FILES:
            shutil.copyfile(HERE / name, root / "scripts" / name)
            assert sha((root / "scripts" / name).read_bytes()) == sha((HERE / name).read_bytes())
        for name, value, uid, gid in [
            ("catalog.json", json.dumps(rig.secrets), 501, 20),
            ("admin", admin, 999, 999),
            ("pgpass", "127.0.0.1:5432:probe:postgres:" + admin + "\n", 999, 999),
            ("pgpass-python", "127.0.0.1:5432:probe:postgres:" + admin + "\n", 501, 20),
        ]:
            p = root / "secrets" / name
            p.write_text(value)
            p.chmod(0o600)
            rig.call(["sudo", "chown", f"{uid}:{gid}", str(p)])
        stage(rig)
        py = "/tools/python/bin/python3.14"
        rig.run("installer", ["-c", py + " /scripts/smoke.py basic"], smoke=True)
        rig.smokes["installer"] = rig.signature("installer")
        rig.run(
            "installer",
            [
                "-c",
                (
                    dlt_install_command()
                    + (
                        " && /work/venv/bin/python -m pip install --no-index --find-links=/tools/artifacts --require-hashes --no-compile -r /scripts/requirements.txt"
                        if held_reader
                        else ""
                    )
                )
                if producer_capture
                else py
                + " -m venv /work/venv && /work/venv/bin/python -m pip install --no-index --find-links=/tools/artifacts --require-hashes --no-compile -r /scripts/requirements.txt",
            ],
        )
        disk_gate(rig, rig.remaining, "after-tiny-install-before-native-diagnostic")
        rig.pg = rig.create("postgres", ["postgres", "-c", "max_wal_size=128MB"])
        rig.call(["docker", "start", rig.pg])
        deadline = time.monotonic() + 90
        while rig.call(
            ["docker", "exec", rig.pg, "pg_isready", "-h", "127.0.0.1"], check=False
        ).returncode:
            assert time.monotonic() < deadline, "Owned PG startup deadline"
            time.sleep(0.25)
        rig.run("producer", ["-c", "/work/venv/bin/python /scripts/smoke.py postgres"], smoke=True)
        rig.smokes["producer"] = rig.signature("producer")
        result["running_python_pg_smoke"] = "PASS"
        for name in [
            "diagnostic.py",
            "dbt_view.py",
            "dbt_assets.py",
            "dbt_publication.py",
            "native_namespaces.py",
        ] + DLT_FILES:
            assert (
                sha((root / "scripts" / name).read_bytes()) == result["qualification_inputs"][name]
            )
        result["native"] = "RAN"
        if held_reader:
            rig.run("producer", ["-c", "/work/venv/bin/python /scripts/diagnostic.py"], timeout=900)
        rig.run(
            "producer",
            [
                "-c",
                "/work/venv/bin/python /scripts/producer_capture.py"
                if producer_capture
                else "/work/venv/bin/python /scripts/diagnostic.py --view-gate --assets-gate --publication-gate --namespaces-gate",
            ],
            timeout=900,
        )
        if producer_capture:
            captured = json.loads((root / "work/producer-capture.json").read_text())
            assert captured["status"] == "PASS_SOURCE_PINNED_DLT_DUCKLAKE_CAPTURE"
            result["producer_capture"] = captured["status"]
            if held_reader:
                result["held_prerequisite"] = prerequisite(
                    root / "work", root / "work/project/target"
                )
        else:
            for file, field, expected in [
                ("dbt-view.json", "dbt_view", "PASS_REAL_DBT_VIEW"),
                ("dbt-assets.json", "dbt_assets", "PASS_REMAINING_DBT_ASSETS"),
                (
                    "dbt-publication.json",
                    "dbt_publication",
                    "PASS_SCRATCH_CHECKED_PUBLICATION_AND_NATIVE_TRANSACTION",
                ),
                (
                    "native-namespaces.json",
                    "native_namespaces",
                    "PASS_NATIVE_ACTOR_CATALOG_INDEPENDENCE",
                ),
            ]:
                proof = json.loads((root / "work" / file).read_text())
                assert proof["status"] == expected
                result[field] = proof["status"]
        disk_gate(rig, 0, "after-native-diagnostic")
        result["status"] = expected_status
    except Exception as error:
        result["error"] = str(error)
    finally:
        try:
            export(rig)
        except Exception as error:
            result["status"], result["export_error"] = "FAIL", str(error)
        try:
            rig.cleanup()
            for role, present in existing.items():
                ref = PINS["images"][role]["ref"]
                if not present and image_present(rig, ref):
                    assert not rig.call(
                        ["docker", "ps", "-aq", "--filter", "ancestor=" + ref]
                    ).stdout.strip()
                    rig.call(["docker", "image", "rm", ref])
                    assert not image_present(rig, ref)
            result["cleanup"] = "PASS"
        except Exception as error:
            result["status"], result["cleanup_error"] = "FAIL", str(error)
        result["remaining_secret_files"] = len(list((root / "secrets").iterdir()))
        result["seconds"] = time.monotonic() - rig.started
        result["finished_utc"] = time.strftime("%FT%TZ", time.gmtime())
        text = json.dumps(result, indent=2)
        assert admin not in text
        (root / "evidence/result.json").write_text(text + "\n")
        rig.save()
    assert result["status"] == expected_status, "Diagnostic failed; sanitized evidence retained"


if __name__ == "__main__":
    main()
