"""tests/unit/test_declared_dependencies.py

Every third-party module imported by dango core code must be declared in pyproject.toml.
"""

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest
import tomlkit

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
# Vendored dlt connectors resolve their own dependencies through dlt.
EXCLUDED_PARTS = {"dlt_sources"}


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared() -> set[str]:
    data = tomlkit.parse((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        _normalize(re.split(r"[\[<>=~!;\s]", dep, maxsplit=1)[0])
        for dep in data["project"]["dependencies"]
    }


def _imported_top_levels() -> dict[str, str]:
    seen: dict[str, str] = {}
    for path in sorted((ROOT / "dango").rglob("*.py")):
        if EXCLUDED_PARTS & set(path.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                if top in sys.stdlib_module_names or top == "dango":
                    continue
                seen.setdefault(top, f"{path.relative_to(ROOT)}:{node.lineno}")
    return seen


def test_every_third_party_import_is_declared() -> None:
    declared = _declared()
    providers = packages_distributions()
    undeclared = {}
    for top, location in _imported_top_levels().items():
        candidates = {_normalize(dist) for dist in providers.get(top, [])}
        candidates.add(_normalize(top))
        if not candidates & declared:
            undeclared[top] = location
    assert not undeclared, f"imported by dango but not in pyproject.toml dependencies: {undeclared}"


@pytest.mark.parametrize("package", ["tzlocal", "packaging"])
def test_core_imports_have_declared_floor(package: str) -> None:
    data = tomlkit.parse((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    specs = [d for d in data["project"]["dependencies"] if _normalize(d).startswith(package)]
    assert specs, f"{package} is imported by core code but not declared"
    assert ">=" in specs[0], f"{package} needs a version floor, got {specs[0]!r}"
