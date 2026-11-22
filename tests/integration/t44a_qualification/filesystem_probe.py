"""tests/integration/t44a_qualification/filesystem_probe.py

Explicit non-native bootstrap, standard acquisition/install, then filesystem observation.
"""

import importlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

from app_acquire import ACQUIRE
from run import PINS, disk_gate, sha

HERE = Path(__file__).resolve().parent
STATUS = "FILESYSTEM_DIAGNOSTIC_PREPARED_NOT_NATIVE"
INPUTS = [
    "filesystem_probe.py",
    "filesystem_links.py",
    "app_acquire.py",
    "app_build.py",
    "pins.json",
    "run.py",
]


def require_preparation(root, baseline):
    """Admit only this explicit diagnostic; never impersonate native prerequisites."""
    report = json.loads((root / "evidence/result.json").read_text())
    assert report["status"] == STATUS and report["qualification_mode"] == "filesystem-probe"
    assert report["native"] == report["full_app"] == "UNRUN"
    assert report["inputs"] == {name: sha((HERE / name).read_bytes()) for name in INPUTS}
    code = baseline / "tests/integration/lightdash_probe"
    assert sha((code / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    assert sha((code / "acquire.py").read_bytes()) == ACQUIRE
    assert json.loads((code / "pins.json").read_text())["images"] == PINS["images"]
    assert not any(p.is_file() for p in (root / "secrets").rglob("*"))
    return report


def main():
    """No PG, warehouse, Python native install, app build or app startup is needed."""
    root, baseline = [Path(p).resolve() for p in sys.argv[1:]]
    assert root.name == "t44a03" and not root.exists()
    code = baseline / "tests/integration/lightdash_probe"
    assert sha((code / "runtime.py").read_bytes()) == PINS["baseline_runtime_sha256"]
    assert sha((code / "acquire.py").read_bytes()) == ACQUIRE
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
    guard = secrets.token_hex(24)
    print("::add-mask::" + guard, flush=True)
    mask = root / "probe-mask"
    mask.write_text(guard)
    mask.chmod(0o600)
    rig = runtime.Rig(root, {"probe_guard": guard})
    rig.filesystem_probe = True
    result = {
        "status": STATUS,
        "qualification_mode": "filesystem-probe",
        "native": "UNRUN",
        "full_app": "UNRUN",
        "head": os.environ["PROBE_HEAD"],
        "inputs": {name: sha((HERE / name).read_bytes()) for name in INPUTS},
        "scope": "Diagnostic bootstrap only; no claimed dbt/producer or warehouse result.",
    }
    failed = False
    try:
        rig.docker_root = Path(
            json.loads(rig.call(["docker", "info", "--format", "{{json .DockerRootDir}}"]).stdout)
        )
        disk_gate(rig, 32 * 1024**2, "before-filesystem-diagnostic-preparation")
        for name in ["source", "tools", "scripts", "fixture"]:
            (root / name / "readiness").write_text("populated\n")
        (root / "fixture/data").mkdir()
        (root / "fixture/data/readiness").write_text("populated\n")
        for name in ["source", "tools", "scripts", "work", "home", "fixture"]:
            rig.call(["sudo", "chown", "-R", "501:20", str(root / name)])
        (root / "evidence/result.json").write_text(json.dumps(result, indent=2) + "\n")
        rig.save()
        require_preparation(root, baseline)
        for program in ["app_acquire.py", "app_build.py"]:
            subprocess.run(
                [
                    sys.executable,
                    str(HERE / program),
                    str(root),
                    str(baseline),
                    "--filesystem-probe",
                ],
                check=True,
                env=rig.env,
            )
    except Exception as error:
        failed = True
        # Preserve an already written bounded observation; otherwise report incomplete setup.
        output = root / "evidence/filesystem-probe.json"
        if not output.exists():
            from filesystem_links import write_receipt

            write_receipt(
                root,
                {
                    "complete": False,
                    "stage": "preparation/acquisition/install",
                    "errors": [str(error)],
                    "native": "UNRUN",
                    "n4": "UNRUN",
                },
                rig.secrets.values(),
            )
        raise
    finally:
        from filesystem_links import write_receipt

        cleanup = {"containers_absent": False, "roots_removed": [], "errors": []}
        try:
            rig.receipts = json.loads((root / "evidence/commands.json").read_text())
            names = {
                r["argv"][3]
                for r in rig.receipts
                if r.get("argv", [])[:3] == ["docker", "create", "--name"]
            }
            for name in names:
                absent = rig.call(["docker", "inspect", name], check=False)
                assert (
                    absent.returncode == 1
                    and absent.stderr.strip() == "Error: No such object: " + name
                )
            cleanup["containers_absent"] = True
            for name in [
                "source",
                "tools",
                "scripts",
                "work",
                "home",
                "fixture",
                "secrets",
                "pgdata",
                "host-home",
                "app-acquisition",
            ]:
                target = root / name
                assert target.parent == root and not target.is_symlink()
                if target.exists():
                    rig.call(["sudo", "rm", "-rf", "--", str(target)], timeout=240)
                assert not os.path.lexists(target)
                cleanup["roots_removed"].append(name)
        except Exception as error:
            cleanup["errors"].append(str(error))
        mask.unlink(missing_ok=True)
        cleanup["mask_absent"] = not mask.exists()
        cleanup["secret_files"] = sum(p.is_file() for p in (root / "secrets").rglob("*"))
        output = root / "evidence/filesystem-probe.json"
        report = (
            json.loads(output.read_text()) if output.exists() else {"complete": False, "errors": []}
        )
        report["cleanup"] = cleanup
        if cleanup["errors"]:
            report["complete"] = False
        final = write_receipt(root, report, rig.secrets.values())
        if not failed:
            assert not cleanup["errors"], "Filesystem diagnostic cleanup incomplete"
            assert final["complete"] and final["status"] == (
                "COMPLETE_FILESYSTEM_OBSERVATION_NOT_N4"
            ), "Filesystem diagnostic final receipt incomplete"


if __name__ == "__main__":
    main()
