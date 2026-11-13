"""tests/unit/test_npm_security_floor.py

Guard the resolved versions of npm build dependencies.
"""

import json
from pathlib import Path

from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def _resolved_versions(package_name: str) -> list[Version]:
    lockfile = json.loads((ROOT / "package-lock.json").read_text())
    return [
        Version(package["version"])
        for path, package in lockfile["packages"].items()
        if path.endswith(f"node_modules/{package_name}")
    ]


def test_all_resolved_postcss_selector_parser_versions_are_patched() -> None:
    versions = _resolved_versions("postcss-selector-parser")
    assert versions
    assert all(version >= Version("7.1.6") for version in versions)


def test_all_resolved_source_map_js_versions_are_patched() -> None:
    versions = _resolved_versions("source-map-js")
    assert versions
    assert all(version >= Version("1.2.2") for version in versions)


def test_postcss_selector_parser_override_is_pinned() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    assert package["overrides"]["postcss-selector-parser"] == "7.1.6"
