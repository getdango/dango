"""tests/integration/t44a_qualification/canvas_prebuild.py

Validate the frozen normal canvas release archive before offline installation.
"""

import io
import json
import tarfile

from run import sha


def load_inventory(directory, profile):
    """Read and bind the exact frozen canvas member inventory."""
    data = (directory / "canvas-prebuild.json").read_bytes()
    assert sha(data) == profile["canvas_prebuild"]["inventory_sha256"]
    return json.loads(data)


def verify_archive(canvas_data, canvas):
    """Require every exact archive file, mode and directory before installation."""
    with tarfile.open(fileobj=io.BytesIO(canvas_data), mode="r:gz") as archive:
        files = {}
        directories = []
        for member in archive.getmembers():
            if member.isdir():
                directories.append(member.name)
            else:
                assert member.isfile() and member.name in canvas["members"]
                files[member.name] = {
                    "sha256": sha(archive.extractfile(member).read()),
                    "bytes": member.size,
                    "mode": member.mode,
                }
        assert sorted(directories) == sorted(canvas["directories"])
        assert files == {
            name: {k: pin[k] for k in ["sha256", "bytes", "mode"]}
            for name, pin in canvas["members"].items()
        }, "Frozen canvas archive members changed"
