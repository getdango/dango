"""tests/integration/t44a_qualification/dlt_source.py

Frozen upstream source build inputs and installed-byte verification, not a resolver.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "dlt-source-pins.json").read_text())
MIB = 1024**2


def artifacts(base):
    """Reuse frozen Python and acquire only this source experiment's exact closure."""
    return [p for p in base["artifacts"] if p["kind"] == "python"] + [
        PINS["source"],
        *PINS["artifacts"],
    ]


def remaining(base):
    """Count archives, three expanded copies, native spill/PG/WAL and evidence copies."""
    value = sum(p["compressed"] + 3 * p["expanded_upper"] for p in artifacts(base))
    value += sum(p["compressed"] + 2 * p["expanded_upper"] for p in base["extensions"].values())
    # Configured spill512MiB, PG256MiB +four128MiB WAL, logs16MiB.
    # Extra384MiB: fixture/PG relations256, original/export reports64, build residual64.
    return value + (512 + 256 + 4 * 128 + 16) * MIB + PINS["extra_fixture_peak"]


def install_command():
    """Use ordinary offline wheel install, then unchanged-source PEP517/Hatchling build."""
    py = "/work/venv/bin/python"
    flags = " --no-index --find-links=/tools/artifacts --require-hashes --no-compile"
    return (
        "/tools/python/bin/python3.14 -m venv /work/venv && "
        + py
        + " -m pip install"
        + flags
        + " -r /scripts/requirements-dlt.txt && mkdir -p /work/dlt-build-temp && "
        + "TMPDIR=/work/dlt-build-temp "
        + py
        + " -m pip install --no-build-isolation"
        + flags
        + " -r /scripts/requirements-dlt-source.txt && "
        + py
        + " -m pip check && "
        + py
        + " /scripts/dlt_source.py"
    )


def verify_install():
    """Bind the genuine source build and native dependency bytes before loading them."""
    from packaging.requirements import Requirement

    dist = importlib.metadata.distribution("dlt")
    assert dist.version == PINS["source"]["version"]
    direct = json.loads(dist.read_text("direct_url.json"))
    assert direct["url"] == "file:///tools/artifacts/" + PINS["source"]["file"]
    assert direct["archive_info"]["hashes"]["sha256"] == PINS["source"]["sha256"]
    actual = {
        str(path): hashlib.sha256(dist.locate_file(path).read_bytes()).hexdigest()
        for path in dist.files
        if str(path).startswith("dlt/") and str(path).endswith(".py")
    }
    assert actual == PINS["package_py_sha256"], "Installed dlt differs from exact source commit"
    for text in dist.requires or []:
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate({"extra": "ducklake"}):
            assert importlib.metadata.version(requirement.name) in requirement.specifier, (
                "Actual source-built DuckLake requirement is not satisfied"
            )
    for pin in PINS["artifacts"]:
        installed = importlib.metadata.distribution(pin["name"])
        assert installed.version == pin["version"], "Frozen dependency version changed"
        for name, expected in {**pin["native_sha256"], **pin["notice_hashes"]}.items():
            assert hashlib.sha256(installed.locate_file(name).read_bytes()).hexdigest() == expected
    result = {
        "status": "PASS_EXACT_UPSTREAM_SOURCE_AND_OFFLINE_CLOSURE",
        "commit": PINS["source_commit"],
        "source_archive_sha256": PINS["source"]["sha256"],
        "version_label": dist.version,
        "not_released_132_wheel": True,
        "package_py_files": len(actual),
        "native_dependency_members": sum(len(p["native_sha256"]) for p in PINS["artifacts"]),
    }
    Path("/work/dlt-install.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result["status"])
    return result


if __name__ == "__main__":
    verify_install()
