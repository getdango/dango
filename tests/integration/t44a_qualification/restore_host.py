"""tests/integration/t44a_qualification/restore_host.py

One serial cold replacement recipe; ordinary launch/audits remain shared and pinned.
"""

import hashlib
import json
import re
import sys
import time
from pathlib import Path

from held_reader import pg_snapshot
from restore_files import REPORT_BOUND, ROOTS, audit_archive, compare
from run import disk_gate

STATUS = "PASS_REPLACEMENT_RESTORE_350_300_PRIVATE_403"
FILES = [
    "restore_host.py",
    "restore_files.py",
    "restore_native.py",
    "restore_actor.cjs",
    "app_runtime_profiles.py",
]
# Original/replacement inventories and both exported copies, plus64MiB other reports.
REPORT_RESERVE = 4 * REPORT_BOUND + 64 * 1024**2
HERE = Path(__file__).resolve().parent


def file_hash(path):
    """Stream large private archives without exposing payload bytes."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def control_identity(text):
    """Bind immutable cluster identity from the exact PostgreSQL control utility."""
    fields = [
        re.search(r"^" + field + r":\s+(\d+)$", text, re.MULTILINE)
        for field in ["Database system identifier", "pg_control version number"]
    ]
    assert all(fields), "Missing PostgreSQL control identity"
    return [field.group(1) for field in fields]


def missing(rig, name):
    """Require canonical exact identity absence, never transport/auth ambiguity."""
    found = rig.call(["docker", "inspect", name], check=False)
    assert found.returncode == 1 and found.stderr.strip() == "Error: No such object: " + name


def retire(rig, name):
    """Forward normal stop, retain exit evidence, refuse forced kill and remove exact ID."""
    before = json.loads(rig.call(["docker", "inspect", name]).stdout)[0]
    assert before["Name"] == "/" + name and before["Config"]["Labels"]["probe.owner"] == rig.label
    assert before["Id"] == rig.objects[name][1]["Id"]
    if before["State"]["Running"]:
        rig.call(["docker", "stop", "--time=30", before["Id"]], timeout=45)
    rig.capture(name)
    after = json.loads(rig.call(["docker", "inspect", before["Id"]]).stdout)[0]
    assert after["Id"] == before["Id"] and after["State"]["Status"] == "exited"
    assert after["State"]["ExitCode"] == 0 and not after["State"]["OOMKilled"], (
        "Non-graceful original stop"
    )
    events = rig.call(
        [
            "docker",
            "events",
            "--since",
            "1h",
            "--until",
            str(int(time.time())),
            "--filter",
            "container=" + before["Id"],
            "--format",
            "{{json .}}",
        ]
    ).stdout
    for line in events.splitlines():
        event = json.loads(line)
        assert not (
            event.get("Action") == "kill"
            and event.get("Actor", {}).get("Attributes", {}).get("signal") in {"9", "SIGKILL"}
        ), "Forced original kill"
    rig.call(["docker", "rm", before["Id"]])
    missing(rig, name)
    missing(rig, before["Id"])
    return {"name": name, "id": before["Id"], "exit": 0, "started_at": after["State"]["StartedAt"]}


def actor(rig, mode):
    """Precreate actor-profile objects while the original PG-only create gate permits it."""
    rig.component = "actor"
    return rig.create(
        "consumer",
        [
            "-c",
            "export NODE_OPTIONS=--max-old-space-size=640; exec node /scripts/restore_actor.cjs "
            + mode,
        ],
    )


def completed(rig, name):
    """Wait for a real actor exit, retaining capture before exact removal."""
    result = rig.call(["docker", "wait", rig.objects[name][1]["Id"]], timeout=600)
    assert result.stdout.strip() == "0", "Restore actor failed; native receipts retained"
    return retire(rig, name)


def private_inventory(rig, top, source, destination, verify=False):
    """Use the same stdlib file inspector under sudo for inaccessible PG999 data."""
    argv = [
        "sudo",
        sys.executable,
        str(HERE / "restore_files.py"),
        str(top),
        str(source),
        str(destination),
    ]
    if verify:
        argv += ["--verify"]
    output = json.loads(rig.call(argv, timeout=600).stdout)
    assert output["sha256"] == file_hash(destination)
    return json.loads(destination.read_text())


def restored_mounts(rig, original_ids, top):
    """Reject any original ID, source prefix, private archive or original root fallback."""
    replacement = top / "restore-replacement"
    assert rig.root == replacement and replacement.resolve(strict=True) == replacement
    for relative in ROOTS:
        rig.call(["sudo", "test", "!", "-e", str(top / relative)])
        assert not (top / relative).exists(), "Original root remains available"
    for identity in original_ids:
        missing(rig, identity)
    for _, (_, frozen) in rig.objects.items():
        assert frozen["Id"] not in original_ids
        for mount in frozen["Mounts"]:
            source = Path(mount["Source"])
            assert source.is_relative_to(replacement) and source.resolve() == source
            assert source.parts[len(replacement.parts)] in ROOTS


def start_pg(rig, original_ids, top):
    """Start an existing clean cluster, with no initdb or database creation path."""
    rig.call(["sudo", "test", "-f", str(rig.root / "pgdata/PG_VERSION")])
    rig.call(["sudo", "test", "!", "-e", str(rig.root / "pgdata/postmaster.pid")])
    rig.component = "postgres"
    rig.pg = rig.create("postgres", ["postgres", "-c", "max_wal_size=128MB"])
    restored_mounts(rig, original_ids, top)
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
            "&& test -w /var/lib/postgresql/data "
            "&& if touch /secrets/must-refuse 2>/dev/null; then exit 1; fi",
        ]
    )


def export(rig, top, paths):
    """Keep both actual inventory maps and sanitized receipts within the charged reserve."""
    size = sum(p.stat().st_size for p in paths if p.is_file())
    assert size * 2 <= REPORT_RESERVE, "Restore report copies exceed reserve"
    disk_gate(rig, size, "before-restore-evidence-export")
    for path in paths:
        if path.is_file():
            body = path.read_bytes()
            assert not any(v.encode() in body for v in rig.secrets.values()), (
                "Restore export credential exposure"
            )
            (
                top
                / "evidence"
                / (path.name if path.name.startswith("restore-") else "restore-" + path.name)
            ).write_bytes(body)
    disk_gate(rig, 0, "after-restore-evidence-export")


def run(original, names, result, growth):
    """Stop original writers, cold-copy, replace, verify existing identities and clean all copies."""
    top = original.root
    report = {
        "status": "FAIL",
        "original_ids": [],
        "replacement_ids": [],
        "phase": "original-baseline",
    }
    replacement = None
    backup = top / "restore-backup"
    quarantine = top / "restore-quarantine"
    target = top / "restore-replacement"
    assert not any(p.exists() for p in [backup, quarantine, target])
    for path in [backup, quarantine, target]:
        path.mkdir(mode=0o700)
    exports = []
    try:
        original.start_owned(names["actor"])
        report["original_ids"].append(completed(original, names["actor"]))
        baseline = json.loads((top / "work/app-slice.json").read_text())
        assert baseline["status"] == "PASS_RESTORE_ORIGINAL_350"
        report["identity"] = baseline["restoreIdentity"]
        report["catalog_before"] = pg_snapshot(original)
        # No app writer remains when the final object list/body inventory is captured.
        report["original_ids"].append(retire(original, names["app"]))
        original.start_owned(names["inventory"])
        report["original_ids"].append(completed(original, names["inventory"]))
        saved = json.loads((top / "work/restore-original.json").read_text())
        assert saved["status"] == "PASS_QUIESCENT_ORIGINAL_STATE"
        exports += [top / "work/app-slice.json", top / "work/restore-original.json"]
        report["garage_node_identity"] = original.call(
            [
                "docker",
                "exec",
                names["garage"],
                "/tools/garage",
                "-c",
                "/scripts/garage.toml",
                "node",
                "id",
                "--quiet",
            ]
        ).stdout.strip()
        report["original_ids"].append(retire(original, names["garage"]))
        report["original_ids"].append(retire(original, original.pg))
        # Existing frozen PG binary/image, no services, proves clean shutdown before tar.
        original.component = "postgres"
        control = original.create("postgres", ["pg_controldata", "/var/lib/postgresql/data"])
        original.start_owned(control, attached=True)
        control_receipt = original.receipts[-1]
        assert control_receipt["argv"] == ["docker", "start", "-a", control]
        output = control_receipt["stdout"]
        report["pg_control_identity"] = control_identity(output)
        assert re.search(r"^Database cluster state:\s+shut down$", output, re.MULTILINE), (
            "PG not cleanly shut down"
        )
        original.call(["sudo", "test", "!", "-e", str(top / "pgdata/postmaster.pid")])
        report["original_ids"].append(retire(original, control))
        for name in original.names:
            missing(original, name)
        report["phase"] = "cold-copy"
        disk_gate(original, growth + REPORT_RESERVE, "before-restore-cold-inventory")
        inv = private_inventory(original, top, top, backup / "original-inventory.json")
        remaining = inv["archive_upper"] + inv["restore_upper"] + growth + REPORT_RESERVE
        report["copy_bounds"] = {k: inv[k] for k in ["archive_upper", "restore_upper"]}
        disk_gate(original, remaining, "before-restore-cold-archive")
        archive = backup / "runtime.tar"
        original.call(["sudo", "install", "-m", "600", "/dev/null", str(archive)])
        original.call(
            [
                "sudo",
                "tar",
                "--numeric-owner",
                "--format=posix",
                "--sparse",
                "-cpf",
                str(archive),
                "-C",
                str(top),
                *ROOTS,
            ],
            timeout=600,
        )
        original.call(["sudo", "chown", f"{top.stat().st_uid}:{top.stat().st_gid}", str(archive)])
        assert archive.stat().st_blocks * 512 <= inv["archive_upper"]
        report["archive_sha256"] = file_hash(archive)
        audit_archive(archive, inv)
        private_inventory(original, top, top, backup / "original-inventory.json", True)
        disk_gate(
            original,
            inv["restore_upper"] + growth + REPORT_RESERVE,
            "before-restore-quarantine-and-extract",
        )
        for relative in ROOTS:
            original.call(["sudo", "mv", "--", str(top / relative), str(quarantine / relative)])
            original.call(["sudo", "chmod", "000", str(quarantine / relative)])
            original.call(["sudo", "test", "!", "-e", str(top / relative)])
            assert not (top / relative).exists()
        original.call(
            [
                "sudo",
                "tar",
                "-xpf",
                str(archive),
                "--same-owner",
                "--same-permissions",
                "--numeric-owner",
                "-C",
                str(target),
            ],
            timeout=600,
        )
        actual = private_inventory(original, top, target, backup / "replacement-inventory.json")
        compare(inv, actual)
        actual_bytes = int(
            original.call(["sudo", "du", "-s", "--block-size=1", str(target)]).stdout.split()[0]
        )
        assert actual_bytes <= inv["restore_upper"], (
            "Actual replacement exceeds measured expansion bound"
        )
        report["actual_replacement_bytes"] = actual_bytes
        disk_gate(
            original, growth + REPORT_RESERVE, "after-complete-independent-restore-extraction"
        )
        report["prestart_complete_cold_inventory_equal"] = True
        exports = [
            target / "work/app-slice.json",
            target / "work/restore-original.json",
            backup / "original-inventory.json",
            backup / "replacement-inventory.json",
        ]
        # Fresh launcher identity/path readiness; no original secret file is consulted.
        # Reference values are used only by the raw-output scanner. Child credentials
        # come exclusively from restored mode0600 files, verified by the inventory.
        original.call(
            [
                "sudo",
                "install",
                "-d",
                "-m",
                "700",
                "-o",
                "501",
                "-g",
                "20",
                str(target / "work/actor-home"),
            ]
        )
        replacement = type(original)(target, dict(original.secrets))
        replacement.label = original.label + "-replacement"
        replacement.receipts = original.receipts
        replacement.images = original.images
        replacement.docker_root = original.docker_root
        replacement.objects, replacement.ready = {}, {}
        replacement.input_hashes = original.input_hashes
        replacement.save = original.save  # One complete sanitized ledger outside mutable roots.
        disk_gate(original, growth + REPORT_RESERVE, "before-existing-replacement-pg")
        original_ids = {r["id"] for r in report["original_ids"]}
        start_pg(replacement, original_ids, top)
        control_after = replacement.call(
            ["docker", "exec", replacement.pg, "pg_controldata", "/var/lib/postgresql/data"]
        ).stdout
        assert control_identity(control_after) == report["pg_control_identity"], (
            "Restored PG system identity"
        )
        assert pg_snapshot(replacement) == report["catalog_before"]
        for component in ["garage", "app", "actor"]:
            replacement.component = component
            heap = {"garage": 128, "app": 2048, "actor": 640}[component]
            replacement.run(
                "consumer",
                [
                    "-c",
                    f"export NODE_OPTIONS=--max-old-space-size={heap}; node /scripts/app_runtime_smoke.cjs {component}",
                ],
                smoke=True,
            )
            replacement.ready[component] = replacement.signature("consumer")
        service = {}
        for component in ["garage", "app"]:
            replacement.component = component
            heap = {"garage": 128, "app": 2048}[component]
            service[component] = replacement.create(
                "consumer",
                [
                    "-c",
                    f"export NODE_OPTIONS=--max-old-space-size={heap}; exec node /scripts/app_service.cjs {component} --restore",
                ],
            )
        preapp, queries = actor(replacement, "pre-app"), actor(replacement, "queries")
        original_ids = {r["id"] for r in report["original_ids"]}
        restored_mounts(replacement, original_ids, top)
        report["phase"] = "replacement-pre-app"
        disk_gate(original, growth + REPORT_RESERVE, "before-restored-garage-object-equivalence")
        replacement.start_owned(service["garage"])
        replacement.start_owned(preapp)
        completed(replacement, preapp)
        assert (
            replacement.call(
                [
                    "docker",
                    "exec",
                    service["garage"],
                    "/tools/garage",
                    "-c",
                    "/scripts/garage.toml",
                    "node",
                    "id",
                    "--quiet",
                ]
            ).stdout.strip()
            == report["garage_node_identity"]
        )
        report["phase"] = "replacement-app"
        disk_gate(original, growth + REPORT_RESERVE, "before-restored-app-and-existing-actors")
        restored_mounts(replacement, original_ids, top)
        replacement.start_owned(service["app"])
        replacement.start_owned(queries)
        completed(replacement, queries)
        final = json.loads((target / "work/restore-replacement.json").read_text())
        assert final["status"] == "PASS_REPLACEMENT_APP_350_300_PRIVATE_403"
        assert pg_snapshot(replacement) == report["catalog_before"]
        report.update(status=STATUS, restored=final, catalog_after=pg_snapshot(replacement))
        exports += [target / "work/restore-pre-app.json", target / "work/restore-replacement.json"]
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        errors = []
        can_delete = True
        for rig in [replacement, original]:
            if rig is not None:
                try:
                    rig.cleanup()
                    for name in rig.names:
                        missing(rig, name)
                    if rig is replacement:
                        report["replacement_ids"] = [info["Id"] for _, info in rig.objects.values()]
                except Exception as error:
                    errors.append(str(error))
        for rig in [replacement, original]:
            if rig is not None:
                for name in rig.names:
                    try:
                        missing(rig, name)
                    except Exception as error:
                        can_delete = False
                        errors.append(str(error))
        try:
            export(original, top, exports)
        except Exception as error:
            errors.append(str(error))
        if can_delete:
            try:
                for path in [backup, quarantine, target, *[top / r for r in ROOTS]]:
                    assert path.parent == top and path.resolve() == path and not path.is_symlink()
                    if path.exists():
                        original.call(["sudo", "rm", "-rf", "--", str(path)])
                    original.call(["sudo", "test", "!", "-e", str(path)])
                    assert not path.exists()
                report["private_inventory_archive_keys_roots_absent"] = True
            except Exception as error:
                errors.append(str(error))
        if not can_delete:
            report["private_roots_retained_unverified_absence"] = True
        if errors:
            report.update(status="FAIL", cleanup_errors=errors)
        text = json.dumps(report, indent=2)
        assert not any(v in text for v in original.secrets.values())
        (top / "evidence/restore-result.json").write_text(text + "\n")
        result.update(
            status=report["status"],
            full_app="PASS_REPLACEMENT_RESTORE" if report["status"] == STATUS else "FAIL_RESTORE",
            restore_private_cleanup="PASS" if not errors else "FAIL",
            restore=report,
        )
    assert report["status"] == STATUS, "Restore failed; phase and native receipts retained"
