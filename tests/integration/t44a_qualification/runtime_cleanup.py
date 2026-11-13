"""tests/integration/t44a_qualification/runtime_cleanup.py

Remove only exact owned runtime credential storage after container absence.
"""

import stat


def cleanup_runtime_storage(rig):
    """Refuse storage deletion until all attempted names and exact Garage IDs are absent."""
    root = rig.root.resolve(strict=True)
    assert root == rig.root, "Task root must already be canonical"
    work = root / "work"
    assert not work.is_symlink() and work.resolve(strict=True) == work
    garage = work / "garage"
    for name in rig.names:
        missing = rig.call(["docker", "inspect", name], check=False)
        assert missing.returncode == 1 and missing.stderr.strip() == (
            "Error: No such object: " + name
        ), "Task container absence is unverified; Garage metadata retained"
    garage_ids = []
    for name, (component, frozen) in rig.objects.items():
        if component == "garage":
            assert frozen["Name"] == "/" + name
            assert frozen["Config"]["Labels"]["probe.owner"] == rig.label
            identity = frozen["Id"]
            missing = rig.call(["docker", "inspect", identity], check=False)
            assert missing.returncode == 1 and missing.stderr.strip() == (
                "Error: No such object: " + identity
            ), "Exact Garage ID absence is unverified"
            garage_ids.append(identity)
    try:
        mode = garage.lstat().st_mode
    except FileNotFoundError:
        pass
    else:
        assert stat.S_ISDIR(mode) and garage.resolve(strict=True) == garage
        rig.call(["sudo", "rm", "-rf", "--", str(garage)])
    try:
        garage.lstat()
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("Garage credential storage remains after removal")
    actor_home = work / "actor-home"
    if actor_home.exists():
        rig.call(["sudo", "rm", "-rf", "--", str(actor_home)])
    return {"garage_storage_absent": True, "verified_garage_ids": garage_ids}
