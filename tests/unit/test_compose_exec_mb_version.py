"""tests/unit/test_compose_exec_mb_version.py

The dango-side reader of ``ENV MB_VERSION`` in the project Dockerfile agrees with Docker:
quotes must balance, and several lines must not disagree or hide an unparseable one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.platform import compose_exec as ce

pytestmark = pytest.mark.unit


def read(tmp_path: Path, text: str) -> str | None:
    path = tmp_path / "Dockerfile.metabase"
    path.write_text(text, encoding="utf-8")
    return ce._project_mb_version(path)


@pytest.mark.parametrize(
    "text",
    [
        "FROM x\nENV MB_VERSION=v0.59.1\n",
        'FROM x\nENV MB_VERSION="v0.59.1"\n',
        "FROM x\nENV MB_VERSION=v0.59.1\nRUN true\nENV MB_VERSION=v0.59.1\n",
    ],
)
def test_valid_forms(tmp_path: Path, text: str) -> None:
    assert read(tmp_path, text) == "v0.59.1"


@pytest.mark.parametrize(
    "text",
    [
        'ENV MB_VERSION="v0.59.1\n',
        'ENV MB_VERSION=v0.59.1"\n',
        "ENV MB_VERSION=v0.59.1\nENV MB_VERSION=v0.60.0\n",
        "ENV MB_VERSION=v0.59.1\nENV MB_VERSION=v0.60.0 EXTRA=1\n",
        "ENV MB_VERSION=v0.59.1\nENV MB_VERSION=$X\n",
        "ENV MB_VERSION=v0.59.1\n  ENV MB_VERSION=v0.60.0\n",
        "ENV MB_VERSION=v0.59.1\nENV A=1 MB_VERSION=v0.60.0\n",
    ],
)
def test_ambiguous_or_unbalanced_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(ce.ComposeRenderError):
        read(tmp_path, text)


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert ce._project_mb_version(tmp_path / "absent") is None
