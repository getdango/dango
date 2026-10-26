"""tests/unit/test_python_support_metadata.py

Check package metadata and CI matrices against the supported Python range.
"""

from pathlib import Path

import tomllib
import yaml
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]


def _project() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def test_python310_is_excluded_from_package_metadata() -> None:
    project = _project()
    assert project["requires-python"] == ">=3.11,<3.14"
    assert "Programming Language :: Python :: 3.10" not in project["classifiers"]
    assert {
        classifier
        for classifier in project["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    } == {f"Programming Language :: Python :: {version}" for version in ("3.11", "3.12", "3.13")}


def test_supported_versions_have_pillow_floor() -> None:
    dependencies = [Requirement(value) for value in _project()["dependencies"]]
    pillow = [dep for dep in dependencies if dep.name.lower() == "pillow"]
    assert len(pillow) == 1
    assert ">=13.0.0" in str(pillow[0].specifier)
    assert Version("12.3.0") not in pillow[0].specifier
    assert Version("13.0.0") in pillow[0].specifier


def test_ci_matrices_match_python_support() -> None:
    ci = _workflow("ci.yml")
    readiness = _workflow("release-readiness.yml")
    assert ci["jobs"]["test"]["name"] == "Test (Python ${{ matrix.python-version }})"
    assert ci["jobs"]["test"]["strategy"]["matrix"]["python-version"] == [
        "3.11",
        "3.12",
        "3.13",
    ]
    for job in ("clean-flow", "wheel-imports"):
        assert readiness["jobs"][job]["strategy"]["matrix"]["python-version"] == [
            "3.11",
            "3.13",
        ]
