"""tests/unit/test_installer_hints.py

Guards the installer re-run hints against the dead get.getdango.dev host.
Hints must use the documented https://getdango.dev/install.(sh|ps1) URLs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEAD_HOST = "get.getdango.dev"


@pytest.mark.unit
@pytest.mark.parametrize("name", ["install.sh", "install.ps1", "README.md"])
def test_no_dead_installer_host(name: str) -> None:
    assert DEAD_HOST not in (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "hint"),
    [
        ("install.sh", "curl -sSL https://getdango.dev/install.sh | bash"),
        ("install.ps1", "irm https://getdango.dev/install.ps1 | iex"),
    ],
)
def test_rerun_hint_uses_working_url(name: str, hint: str) -> None:
    assert hint in (ROOT / name).read_text(encoding="utf-8")
