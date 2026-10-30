"""tests/integration/lightdash_probe/runtime.py

bounded CI profiles and evidence.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

GIB = 1024**3
HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "pins.json").read_text())


class Rig:
    """Own the declared containers and capture their evidence before cleanup."""

    def __init__(self, root, secrets):
        self.root = Path(root)
        self.secrets = secrets
        self.names = []
        self.receipts = []
        self.images = {}
        self.label = f"lightdash-r08-{os.getpid()}"
        self.started = time.monotonic()
        self.smokes = {}
        (self.root / "host-home").mkdir(exist_ok=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.root / "host-home"),
            "LANG": "C.UTF-8",
            "CI": "true",
            "NODE_OPTIONS": "--max-old-space-size=2048",
            "npm_config_userconfig": "/dev/null",
        }

    def save(self):
        """Export only sanitized command receipts and the test result."""
        text = json.dumps(self.receipts, indent=2)
        for value in self.secrets.values():
            assert value not in text, "Evidence secret leak"
        (self.root / "evidence" / "commands.json").write_text(text + "\n")

    def call(self, argv, timeout=120, check=True):
        """Capture output quietly; a raw secret leak makes the operation fail."""
        start = time.monotonic()
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=self.env
            )
        except subprocess.TimeoutExpired as error:

            def decoded(value):
                return value.decode(errors="replace") if isinstance(value, bytes) else value or ""

            result = subprocess.CompletedProcess(
                argv, 124, decoded(error.stdout), decoded(error.stderr) + "\nTIMEOUT"
            )
        leaked = any(s in result.stdout + result.stderr for s in self.secrets.values())
        receipt = {
            "argv": argv,
            "exit": result.returncode,
            "seconds": time.monotonic() - start,
            "captured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stdout": result.stdout,
            "stderr": result.stderr,
            "raw_secret_leak": leaked,
        }
        for key in ["stdout", "stderr"]:
            for value in self.secrets.values():
                receipt[key] = receipt[key].replace(value, "[REDACTED]")
        self.receipts.append(receipt)
        self.save()
        assert not leaked, "Raw subprocess secret exposure; sanitized evidence retained"
        if check:
            assert result.returncode == 0, f"Command failed: {argv[0:3]}"
        return result

    def inspect_image(self, role):
        """Bind the frozen manifest, config, platform and ordered layers."""
        pin = PINS["images"][role]
        self.call(["docker", "pull", "--platform=linux/amd64", pin["ref"]], timeout=240)
        info = json.loads(self.call(["docker", "image", "inspect", pin["ref"]]).stdout)[0]
        assert info["Os"] == "linux" and info["Architecture"] == "amd64"
        assert info["RootFS"]["Layers"] == pin["diff_ids"]
        manifest_bytes = (HERE / "image-metadata" / f"{role}-manifest.json").read_bytes()
        config_bytes = (HERE / "image-metadata" / f"{role}-config.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        config = json.loads(config_bytes)
        manifest_digest = pin["ref"].split("@")[1]
        assert "sha256:" + hashlib.sha256(manifest_bytes).hexdigest() == manifest_digest
        assert (
            "sha256:" + hashlib.sha256(config_bytes).hexdigest()
            == manifest["config"]["digest"]
            == pin["config"]
        )
        assert config["rootfs"]["diff_ids"] == pin["diff_ids"]
        assert config["os"] == "linux" and config["architecture"] == "amd64"
        if info["Id"] != pin["config"]:
            # Docker's containerd store can expose manifest identity as Id.
            # Require its descriptor plus the exact frozen config/layer binding.
            assert info["Id"] == manifest_digest
            assert info.get("Descriptor", {}).get("digest") == manifest_digest
        assert pin["ref"] in info["RepoDigests"], "Frozen manifest binding missing"
        self.images[role] = info
        return info

    def argv(self, role, command):
        """Use the identical declarative launch path for smoke and actual work."""
        pg = role == "postgres"
        name = f"{self.label}-{role}-{len(self.names)}"
        memory = 768 * 1024**2 if pg else 3 * GIB
        network = "none" if pg or role == "installer" else "container:" + self.pg
        args = [
            "docker",
            "create",
            "--name",
            name,
            "--label",
            "probe.owner=" + self.label,
            "--platform=linux/amd64",
            "--user",
            "999:999" if pg else "501:20",
            "--cpus",
            "1" if pg else "3",
            "--memory",
            str(memory),
            "--memory-swap",
            str(memory),
            "--pids-limit",
            "128",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--network",
            network,
            "--log-driver=json-file",
            "--log-opt=max-size=8m",
            "--log-opt=max-file=1",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864,mode=1777",
            "--env",
            "HOME=/home/probe",
            "--env",
            "AWS_EC2_METADATA_DISABLED=true",
            "--env",
            "GCE_METADATA_DISABLED=true",
            "--env",
            "NODE_OPTIONS=--max-old-space-size=2048",
        ]
        mounts = [("secrets", "/secrets", True)]
        if pg:
            mounts += [("pgdata", "/var/lib/postgresql/data", False)]
            args += [
                "--tmpfs",
                "/var/run/postgresql:rw,noexec,nosuid,nodev,size=16777216,uid=999,gid=999",
                "--env",
                "POSTGRES_PASSWORD_FILE=/secrets/admin",
                "--env",
                "POSTGRES_DB=probe",
                "--env",
                "POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256",
                "--env",
                "PGPASSFILE=/secrets/pgpass",
            ]
        else:
            mounts += [
                ("source", "/source", True),
                ("tools", "/tools", True),
                ("scripts", "/scripts", True),
                ("work", "/work", False),
                ("home", "/home/probe", False),
                ("fixture", "/fixture", role != "producer"),
            ]
            args += ["--workdir=/work", "--entrypoint=/bin/sh"]
        for source, target, ro in mounts:
            args += [
                "--mount",
                f"type=bind,src={self.root / source},dst={target}" + (",readonly" if ro else ""),
            ]
        args += [PINS["images"]["postgres" if pg else "node"]["ref"], *command]
        return name, args, mounts, network, memory

    def shared_network(self, actual):
        """Bind a shared namespace to the exact created, owned, running PG ID."""
        rows = json.loads(self.call(["docker", "inspect", self.pg]).stdout)
        assert len(rows) == 1, "Expected exactly one owned PG identity"
        pg = rows[0]
        assert hasattr(self, "pg_id") and pg.get("Id") == self.pg_id, "Owned PG ID changed/missing"
        assert len(self.pg_id) == 64 and all(c in "0123456789abcdef" for c in self.pg_id)
        assert pg.get("Name") == "/" + self.pg, "Owned PG name mismatch"
        assert pg["Config"]["Labels"].get("probe.owner") == self.label, "Owned PG label mismatch"
        assert pg["Image"] == self.images["postgres"]["Id"], "Owned PG image mismatch"
        assert pg["Config"]["Image"] == PINS["images"]["postgres"]["ref"], (
            "Frozen PG reference mismatch"
        )
        assert pg["Config"]["User"] == "999:999", "Owned PG user mismatch"
        state = pg["State"]
        assert state["Running"] is True and state["Status"] == "running", "Owned PG not running"
        assert not any(state.get(k) for k in ["Paused", "Restarting", "Dead"]), (
            "Owned PG unavailable"
        )
        assert pg["HostConfig"]["NetworkMode"] == "none", "Owned PG has external network"
        assert not pg["HostConfig"]["PortBindings"], "Owned PG has published ports"
        assert actual == "container:" + self.pg_id, "Shared network is not the exact owned PG ID"

    def create(self, role, command):
        """Inspect persisted limits and mounts before any container starts."""
        name, args, mounts, network, memory = self.argv(role, command)
        self.names.append(name)  # Also inspect/clean partial create failures.
        created_id = self.call(args).stdout.strip()
        info = json.loads(self.call(["docker", "inspect", name]).stdout)[0]
        h = info["HostConfig"]
        assert not info["State"]["Running"] and h["ReadonlyRootfs"]
        assert h["Memory"] == h["MemorySwap"] == memory
        assert h["NanoCpus"] == (10**9 if role == "postgres" else 3 * 10**9)
        expected_tmpfs = {"/tmp": "rw,noexec,nosuid,nodev,size=67108864,mode=1777"}
        if role == "postgres":
            expected_tmpfs["/var/run/postgresql"] = (
                "rw,noexec,nosuid,nodev,size=16777216,uid=999,gid=999"
            )
        assert h["Tmpfs"] == expected_tmpfs
        assert not h["Privileged"] and not h["Devices"] and not h["CapAdd"]
        assert h["PidMode"] == "" and h["IpcMode"] == "private"
        assert h["PidsLimit"] == 128 and h["CapDrop"] == ["ALL"]
        assert h["SecurityOpt"] == ["no-new-privileges:true"]
        if role in {"producer", "consumer"}:
            assert network == "container:" + self.pg
            self.shared_network(h["NetworkMode"])
        else:
            assert h["NetworkMode"] == network
        assert not h["PortBindings"]
        assert h["LogConfig"] == {
            "Type": "json-file",
            "Config": {"max-size": "8m", "max-file": "1"},
        }
        assert info["Config"]["User"] == ("999:999" if role == "postgres" else "501:20")
        assert {(m["Source"], m["Destination"], m["RW"]) for m in info["Mounts"]} == {
            (str(self.root / s), t, not ro) for s, t, ro in mounts
        }
        assert info["Config"]["Labels"]["probe.owner"] == self.label
        expected = self.images["postgres" if role == "postgres" else "node"]["Id"]
        assert info["Image"] == expected
        running = self.call(
            ["docker", "ps", "-q", "--filter", "label=probe.owner=" + self.label]
        ).stdout.split()
        assert len(running) <= (0 if role in {"installer", "postgres"} else 1), (
            "Aggregate profile limit"
        )
        if running:
            other = json.loads(self.call(["docker", "inspect", running[0]]).stdout)[0]
            assert other["Name"] == "/" + self.pg and other["Config"]["User"] == "999:999"
        if role == "postgres":
            assert info["Id"] == created_id and len(created_id) == 64
            assert all(c in "0123456789abcdef" for c in created_id), "Invalid created PG ID"
            self.pg_id = created_id
        return name

    def run(self, role, command, timeout=900, smoke=False):
        """Require fresh representative smoke before expensive/native commands."""
        if not smoke:
            self.require_smoke(role)
        name = self.create(role, command)
        try:
            return self.call(["docker", "start", "-a", name], timeout=timeout)
        finally:
            try:
                self.capture(name)
            finally:
                self.remove(name)

    def signature(self, role):
        """Bind smoke to flags, image IDs, path ownership and top-level permissions."""
        _, args, mounts, _, _ = self.argv(role, [])
        args[3] = "<owned-name>"
        states = [
            (
                s,
                t,
                ro,
                (self.root / s).stat().st_uid,
                (self.root / s).stat().st_gid,
                (self.root / s).stat().st_mode,
            )
            for s, t, ro in mounts
        ]
        return hashlib.sha256(
            json.dumps([args, states, self.images, PINS["tools"]], sort_keys=True).encode()
        ).hexdigest()

    def require_smoke(self, role):
        """Fail closed on missing, failed or stale smoke evidence."""
        assert self.smokes.get(role) == self.signature(role), "MISSING/FAIL/STALE readiness smoke"

    def capture(self, name):
        """Capture complete state, configuration, logs and events before removal."""
        commands = [
            ["docker", "inspect", name],
            ["docker", "logs", "--timestamps", name],
            [
                "docker",
                "events",
                "--since",
                "1h",
                "--until",
                str(int(time.time())),
                "--filter",
                "container=" + name,
                "--format",
                "{{json .}}",
            ],
        ]
        errors = []
        for command in commands:
            try:
                self.call(command, check=False)
            except Exception as error:
                errors.append(str(error))
        assert not errors, "Capture failures: " + "; ".join(errors)

    def remove(self, name):
        """Remove only verified owned containers; verify canonical absence."""
        result = self.call(["docker", "inspect", name], check=False)
        if result.returncode:
            assert result.stderr.strip() == "Error: No such object: " + name
            return
        info = json.loads(result.stdout)[0]
        assert info["Name"] == "/" + name and info["Config"]["Labels"]["probe.owner"] == self.label
        if info["State"]["Running"]:
            self.call(["docker", "stop", "--time=10", info["Id"]])
        self.call(["docker", "rm", info["Id"]])
        result = self.call(["docker", "inspect", name], check=False)
        assert result.returncode == 1 and result.stderr.strip() == "Error: No such object: " + name

    def cleanup(self):
        """Capture then remove every exact owned partial or running object."""
        errors = []
        for name in reversed(self.names):
            for operation in [self.capture, self.remove]:
                try:
                    operation(name)
                except Exception as error:
                    errors.append(str(error))
        for p in (self.root / "secrets").glob("*"):
            try:
                self.call(["sudo", "rm", "--", str(p)])
            except Exception as error:
                errors.append(str(error))
        self.save()
        assert not errors, "Cleanup failures: " + "; ".join(errors)
