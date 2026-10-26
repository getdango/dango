"""tests/unit/test_python_support_text.py

Python support claims in public and newly generated project text.
"""

import re
import tomllib
from pathlib import Path

from dango.cli.init import ProjectInitializer

ROOT = Path(__file__).resolve().parents[2]


def test_generated_compatibility_python_range(tmp_path: Path) -> None:
    initializer = ProjectInitializer(tmp_path)
    initializer._create_compatibility_md()
    compatibility = (tmp_path / "COMPATIBILITY.md").read_text()
    assert "Python 3.11, 3.12, or 3.13 (`>=3.11,<3.14`)" in compatibility
    assert "Python 3.10" not in compatibility
    assert "does not yet support Python 3.13+" not in compatibility
    assert "Python 3.14+ is not yet supported" in compatibility


def test_existing_compatibility_file_is_preserved(tmp_path: Path) -> None:
    compatibility_path = tmp_path / "COMPATIBILITY.md"
    compatibility_path.write_text("Custom project guidance\n")
    ProjectInitializer(tmp_path)._create_compatibility_md()
    assert compatibility_path.read_text() == "Custom project guidance\n"


def test_public_python_range_matches_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    required = re.fullmatch(r">=3\.(\d+),<3\.(\d+)", project["requires-python"])
    assert required is not None
    minimum, exclusive_maximum = map(int, required.groups())

    readme = (ROOT / "README.md").read_text()
    quick_start = readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
    prerequisite = re.search(r"\*\*Prerequisites:\*\* Python 3\.(\d+)-3\.(\d+),", quick_start)
    assert prerequisite is not None
    assert tuple(map(int, prerequisite.groups())) == (minimum, exclusive_maximum - 1)
