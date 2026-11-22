"""tests/integration/t44a_qualification/app_runtime_profiles.py

Unchanged full-app launch and active-state audits shared with replacement restore.
"""

import json

from app_build import verify_build_update
from run import sha

MIB = 1024**2
# Each service retains the frozen image/user/mount/security/network profile.
# These are running limits, not permission to overlap build or fixture production.
PROFILES = {
    "postgres": (0.5, 512, None),
    "fixture": (3, 3072, None),
    "garage": (0.25, 256, 128),
    "app": (2.25, 2560, 2048),
    "actor": (1, 1024, 640),
}


def active_host_config(config):
    """Docker reports enabled OOM killing as either false or null after start."""
    value = config["OomKillDisable"]
    assert value is False or value is None, "Active container OOM killer must remain enabled"
    normalized = dict(config)
    normalized["OomKillDisable"] = False
    return normalized


def verify_active_config(info, frozen, pg, pg_id):
    """Bind Docker's started shared-namespace hostname to the exact owned PG."""
    assert info["Id"] == frozen["Id"], "Active container identity changed"
    assert pg["Id"] == pg_id, "Hostname peer is not the exact owned PG"
    assert pg["Config"]["Hostname"] == pg_id[:12], "Unexpected owned PG hostname"
    expected = dict(frozen["Config"])
    assert expected["Hostname"] == frozen["Id"][:12], "Unexpected custom created hostname"
    if frozen["Id"] != pg_id:
        assert (
            info["HostConfig"]["NetworkMode"]
            == frozen["HostConfig"]["NetworkMode"]
            == ("container:" + pg_id)
        ), "Active hostname is not bound to the exact owned PG namespace"
        expected["Hostname"] = pg["Config"]["Hostname"]
    changed = sorted(
        k
        for k in set(info["Config"]) | set(expected)
        if k not in info["Config"] or k not in expected or info["Config"][k] != expected[k]
    )
    assert not changed, "Active container Config changed: " + ", ".join(changed)


def runtime_rig(base):
    """Reuse launch/capture/cleanup; audit only exact owned nonrunning limit updates."""

    class RuntimeRig(base):
        component = "postgres"

        def create(self, role, command):
            expected_role = {"postgres": "postgres", "fixture": "producer"}.get(
                self.component, "consumer"
            )
            assert role == expected_role
            # The original create gate remains: only PG may be running here.
            name = super().create(role, command)
            before = json.loads(self.call(["docker", "inspect", name]).stdout)[0]
            cpus, memory, _ = PROFILES[self.component]
            self.call(
                [
                    "docker",
                    "update",
                    "--cpus=" + str(cpus),
                    "--memory=" + str(memory * MIB),
                    "--memory-swap=" + str(memory * MIB),
                    before["Id"],
                ]
            )
            after = json.loads(self.call(["docker", "inspect", before["Id"]]).stdout)[0]
            verify_build_update(before, after, cpus, memory * MIB)
            self.objects[name] = (self.component, after)
            return name

        def signature(self, role):
            return sha(
                (
                    super().signature(role)
                    + json.dumps(
                        [self.component, PROFILES[self.component], self.input_hashes],
                        sort_keys=True,
                    )
                ).encode()
            )

        def start_owned(self, name, attached=False):
            component, frozen = self.objects[name]
            self.component = component
            role = {"postgres": "postgres", "fixture": "producer"}.get(component, "consumer")
            if component != "postgres":
                assert self.ready[component] == self.signature(role), "STALE runtime readiness"
            now = json.loads(self.call(["docker", "inspect", name]).stdout)[0]
            cpus, memory, _ = PROFILES[component]
            verify_build_update(frozen, now, cpus, memory * MIB)
            if component != "postgres":
                self.shared_network(now["HostConfig"]["NetworkMode"])
            running = self.call(
                ["docker", "ps", "-q", "--no-trunc", "--filter", "label=probe.owner=" + self.label]
            ).stdout.split()
            known = {info["Id"]: (n, c) for n, (c, info) in self.objects.items()}
            active = []
            for identity in running:
                assert identity in known, "Unregistered running owned identity"
                other_name, other_component = known[identity]
                info = json.loads(self.call(["docker", "inspect", identity]).stdout)[0]
                assert info["Name"] == "/" + other_name
                assert info["Config"]["Labels"]["probe.owner"] == self.label
                assert (
                    info["Id"] == identity and info["Image"] == self.objects[other_name][1]["Image"]
                )
                assert info["State"]["Running"] and info["State"]["Status"] == "running"
                assert sorted(json.dumps(m, sort_keys=True) for m in info["Mounts"]) == sorted(
                    json.dumps(m, sort_keys=True) for m in self.objects[other_name][1]["Mounts"]
                )
                assert active_host_config(info["HostConfig"]) == active_host_config(
                    self.objects[other_name][1]["HostConfig"]
                ), "Active container HostConfig changed beyond Docker's enabled-OOM default"
                verify_active_config(
                    info, self.objects[other_name][1], self.objects[self.pg][1], self.pg_id
                )
                active.append(other_component)
            active.append(component)
            assert len(active) == len(set(active)), "Duplicate running service role"
            assert sum(PROFILES[c][0] for c in active) <= 4
            assert sum(PROFILES[c][1] for c in active) <= 4608
            self.call(["docker", "start", *(["-a"] if attached else []), name], timeout=600)

    return RuntimeRig
