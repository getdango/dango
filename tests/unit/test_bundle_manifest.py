"""tests/unit/test_bundle_manifest.py

Tests for strict bundle manifests, qualification refusal, and packaged resources.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import warnings
import zipfile
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from dango.platform.bundle import (
    BundleComponent,
    BundleManifest,
    BundleProfile,
    BundleValidationError,
    load_bundle_manifest,
    require_qualified_profile,
)

ROOT = Path(__file__).resolve().parents[2]
RESOURCE = ROOT / "dango/templates/bundle-candidates.json"

# Runs in fresh interpreters, before importing dango.platform or the loader.
# Patching the launch/network boundaries makes any attempted call fail loudly.
IMPORT_PROBE = """
import json, os, socket, subprocess, sys
from contextlib import ExitStack
from unittest.mock import patch

wheel = sys.argv[1] if len(sys.argv) > 1 else None
if wheel:
    sys.path.insert(0, wheel)
def forbidden(*args, **kwargs):
    raise AssertionError('native or network operation during import/load')
with ExitStack() as guards:
    for target in ('subprocess.Popen', 'os.system', 'socket.create_connection',
                   'socket.socket.connect', 'socket.socket.connect_ex'):
        guards.enter_context(patch(target, side_effect=forbidden))
    import dango
    import dango.platform.bundle as bundle
    manifest = bundle.load_bundle_manifest()
    if wheel:
        assert dango.__file__.startswith(wheel + '/'), dango.__file__
        assert bundle.__file__.startswith(wheel + '/'), bundle.__file__
        assert type(bundle.__loader__).__name__ == 'zipimporter'
    print(json.dumps({'package_origin': dango.__file__, 'module_origin': bundle.__file__,
                      'manifest': manifest.model_dump()}))
"""


def _isolated_environment(home: Path) -> dict[str, str]:
    """Pass only local execution settings, with no inherited credentials/config."""
    home.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.pathsep.join((str(Path(sys.executable).parent), "/usr/bin", "/bin")),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "TMPDIR": str(home),
        "DO_NOT_TRACK": "1",
        "DANGO_TELEMETRY": "0",
        "PIP_NO_INDEX": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def _write_manifest(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def qualified_fixture() -> dict[str, Any]:
    """Synthetic schema/admission fixture; no real native qualification is claimed."""
    components = [
        {
            "component_id": cid,
            "version": version,
            "artifact_ref": f"fixture://immutable/{cid}/{version}",
            "sha256": digest * 64,
            "edition": "synthetic",
            "license_expression": "MIT",
            "evidence_refs": [f"fixture://evidence/{cid}/receipt-1"],
            "status": "qualified_bundle",
        }
        for cid, version, digest in (("python-client", "1.5.6", "a"), ("node-client", "1.5.2", "b"))
    ]
    return {
        "schema_version": 1,
        "bundle_id": "synthetic-qualified-fixture",
        "evidence_date": "2026-10-15",
        "components": components,
        "profiles": [
            {
                "profile_id": "synthetic-linux",
                "os": "linux",
                "architecture": "x86_64",
                "component_ids": ["python-client", "node-client"],
                "status": "qualified",
                "qualification_receipt": "fixture://immutable/qualification/receipt-1",
            }
        ],
    }


@pytest.mark.unit
class TestBundleManifest:
    def test_default_resource_loads_from_package(self) -> None:
        manifest = load_bundle_manifest()
        assert manifest.schema_version == 1
        assert manifest.bundle_id
        assert manifest.components and manifest.profiles

    def test_candidate_profile_is_not_admitted(self, qualified_fixture: dict[str, Any]) -> None:
        # Use an explicit candidate fixture: shipped profiles may be qualified later.
        qualified_fixture["profiles"][0]["status"] = "candidate"
        qualified_fixture["profiles"][0]["qualification_receipt"] = None
        manifest = BundleManifest.model_validate(qualified_fixture)
        with pytest.raises(BundleValidationError) as caught:
            require_qualified_profile(manifest, "synthetic-linux")
        assert caught.value.code == "unqualified_profile"
        assert caught.value.action
        with pytest.raises(BundleValidationError) as unknown:
            require_qualified_profile(manifest, "secret-unknown-profile")
        assert unknown.value.code == "unsupported_profile"
        assert "secret-unknown-profile" not in str(unknown.value)

    @pytest.mark.parametrize("text", ["", "  ", "null", "[]", "{", '{"secret":"sentinel"}'])
    def test_missing_null_or_invalid_manifest_is_actionable(
        self, tmp_path: Path, text: str
    ) -> None:
        path = tmp_path / "secret-filename.json"
        path.write_text(text)
        with pytest.raises(BundleValidationError) as caught:
            load_bundle_manifest(path)
        assert caught.value.code in {"empty_manifest", "invalid_manifest"}
        assert caught.value.action
        assert "secret" not in str(caught.value) and "sentinel" not in str(caught.value)
        assert caught.value.__context__ is None or caught.value.__suppress_context__
        with pytest.raises(BundleValidationError, match="missing_manifest"):
            load_bundle_manifest(tmp_path / "absent.json")
        with patch.object(Path, "read_text", side_effect=PermissionError("secret-sentinel")):
            with pytest.raises(BundleValidationError, match="unreadable_manifest") as unreadable:
                load_bundle_manifest(path)
        assert "secret-sentinel" not in str(unreadable.value)
        path.write_bytes(b"\xff")
        with pytest.raises(BundleValidationError, match="unreadable_manifest"):
            load_bundle_manifest(path)

    @pytest.mark.parametrize("case", ["component", "profile", "reference", "evidence", "unknown"])
    def test_duplicate_or_unknown_component_ref_is_rejected(
        self, tmp_path: Path, qualified_fixture: dict[str, Any], case: str
    ) -> None:
        if case == "component":
            qualified_fixture["components"].append(deepcopy(qualified_fixture["components"][0]))
        elif case == "profile":
            qualified_fixture["profiles"].append(deepcopy(qualified_fixture["profiles"][0]))
        elif case == "evidence":
            refs = qualified_fixture["components"][0]["evidence_refs"]
            refs.append(refs[0])
        else:
            qualified_fixture["profiles"][0]["component_ids"].append(
                "python-client" if case == "reference" else "absent-component"
            )
        with pytest.raises(BundleValidationError, match="invalid_manifest"):
            load_bundle_manifest(_write_manifest(tmp_path, qualified_fixture))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("sha256", "A" * 64),
            ("sha256", "a" * 63),
            ("sha256", "z" * 64),
            ("sha256", 123),
            ("version", 1.5),
            ("version", True),
            ("status", "success"),
            ("component_id", " "),
        ],
    )
    def test_bad_hash_or_coerced_version_is_rejected(
        self, tmp_path: Path, qualified_fixture: dict[str, Any], field: str, value: Any
    ) -> None:
        qualified_fixture["components"][0][field] = value
        with pytest.raises(BundleValidationError, match="invalid_manifest"):
            load_bundle_manifest(_write_manifest(tmp_path, qualified_fixture))

    @pytest.mark.parametrize("value", [True, "1", 1.0, 2, None])
    def test_schema_version_is_exact_integer(
        self, qualified_fixture: dict[str, Any], value: Any
    ) -> None:
        qualified_fixture["schema_version"] = value
        with pytest.raises(ValidationError):
            BundleManifest.model_validate(qualified_fixture)

    @pytest.mark.parametrize(
        "field",
        ["version", "artifact_ref", "sha256", "license_expression", "evidence_refs", "receipt"],
    )
    def test_qualified_fixture_requires_all_artifacts_and_receipt(
        self, tmp_path: Path, qualified_fixture: dict[str, Any], field: str
    ) -> None:
        manifest = load_bundle_manifest(_write_manifest(tmp_path, qualified_fixture))
        admitted = require_qualified_profile(manifest, "synthetic-linux")
        assert admitted.component_ids == ["python-client", "node-client"]
        # A string receipt is intentionally sufficient only for schema admission.
        assert admitted.qualification_receipt == "fixture://immutable/qualification/receipt-1"
        if field == "receipt":
            qualified_fixture["profiles"][0]["qualification_receipt"] = None
        else:
            qualified_fixture["components"][1][field] = [] if field == "evidence_refs" else None
        with pytest.raises(BundleValidationError, match="invalid_manifest"):
            load_bundle_manifest(_write_manifest(tmp_path, qualified_fixture))

    @pytest.mark.parametrize("status", ["observed_component", "candidate"])
    def test_observed_components_do_not_qualify_bundle(
        self, qualified_fixture: dict[str, Any], status: str
    ) -> None:
        qualified_fixture["components"][1]["status"] = status
        with pytest.raises(ValidationError):
            BundleManifest.model_validate(qualified_fixture)

    @pytest.mark.parametrize("record", ["manifest", "component", "profile"])
    def test_records_are_strict_frozen_and_require_fields(
        self, qualified_fixture: dict[str, Any], record: str
    ) -> None:
        model, data = {
            "manifest": (BundleManifest, qualified_fixture),
            "component": (BundleComponent, qualified_fixture["components"][0]),
            "profile": (BundleProfile, qualified_fixture["profiles"][0]),
        }[record]
        instance = model.model_validate(data)
        with pytest.raises(ValidationError):
            instance.schema_version = 2
        with pytest.raises(ValidationError):
            model.model_validate({**data, "unexpected": "secret-sentinel"})
        for field in data:
            incomplete = {key: value for key, value in data.items() if key != field}
            with pytest.raises(ValidationError):
                model.model_validate(incomplete)

    def test_mutated_lists_and_unchecked_models_cannot_bypass_admission(
        self, qualified_fixture: dict[str, Any]
    ) -> None:
        manifest = BundleManifest.model_validate(qualified_fixture)
        manifest.components[0].evidence_refs.clear()
        with pytest.raises(BundleValidationError, match="invalid_manifest"):
            require_qualified_profile(manifest, "synthetic-linux")
        qualified_fixture["profiles"][0]["qualification_receipt"] = None
        unchecked = BundleManifest.model_construct(**qualified_fixture)
        with warnings.catch_warnings(record=True) as recorded:
            with pytest.raises(BundleValidationError, match="invalid_manifest"):
                require_qualified_profile(unchecked, "synthetic-linux")
        assert not recorded

    @pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity", "1e999"])
    def test_nonfinite_numbers_and_duplicate_json_keys_are_refused(
        self, tmp_path: Path, qualified_fixture: dict[str, Any], token: str
    ) -> None:
        text = json.dumps(qualified_fixture).replace(
            '"schema_version": 1', f'"schema_version": {token}'
        )
        path = tmp_path / "manifest.json"
        path.write_text(text)
        with pytest.raises(BundleValidationError, match="invalid_manifest"):
            load_bundle_manifest(path)
        path.write_text('{"schema_version": 0,' + json.dumps(qualified_fixture)[1:])
        with pytest.raises(BundleValidationError, match="invalid_manifest"):
            load_bundle_manifest(path)

    def test_explicit_path_and_packaged_resource_match(self) -> None:
        assert load_bundle_manifest(RESOURCE) == load_bundle_manifest()

    def test_import_and_load_make_no_native_or_network_calls(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-c", IMPORT_PROBE],
            cwd=ROOT,
            env=_isolated_environment(tmp_path / "home"),
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(result.stdout)
        assert Path(report["module_origin"]) == ROOT / "dango/platform/bundle.py"
        assert report["manifest"]["schema_version"] == 1

    def test_wheel_contains_bundle_resource(self, tmp_path: Path) -> None:
        wheel_dir = tmp_path / "wheels"
        wheel_dir.mkdir()
        environment = _isolated_environment(tmp_path / "home")
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheel_dir),
                ".",
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        assert built.returncode == 0, built.stdout + built.stderr
        wheels = list(wheel_dir.glob("*.whl"))
        assert len(wheels) == 1
        wheel = wheels[0]
        with zipfile.ZipFile(wheel) as archive:
            members = archive.namelist()
            assert "dango/templates/bundle-candidates.json" in members
            assert "dango/platform/bundle.py" in members
            assert json.loads(archive.read("dango/templates/bundle-candidates.json")) == json.loads(
                RESOURCE.read_text()
            )
        # -I excludes cwd/PYTHONPATH/user-site. The empty cwd contains no source.
        # Existing environment site-packages supply dependencies, without installing.
        outside_source = tmp_path / "zip-import"
        outside_source.mkdir()
        loaded = subprocess.run(
            [sys.executable, "-I", "-c", IMPORT_PROBE, str(wheel)],
            cwd=outside_source,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        report = json.loads(loaded.stdout)
        assert report["manifest"] == load_bundle_manifest().model_dump()
        print(
            json.dumps(
                {
                    "wheel": str(wheel),
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "bundle_members": [name for name in members if "bundle" in name],
                    "zip_package_origin": report["package_origin"],
                    "zip_module_origin": report["module_origin"],
                },
                sort_keys=True,
            )
        )
