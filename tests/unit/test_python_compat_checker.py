"""tests/unit/test_python_compat_checker.py

Verify the direct dependency Python compatibility guard.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

from packaging.version import Version

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_python_compat.py"


def _checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_python_compat", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Distribution:
    def __init__(self, requires_python: str) -> None:
        self.metadata = {
            "Name": "matplotlib",
            "Version": "3.11.0",
            "Requires-Python": requires_python,
        }


def test_checker_targets_311_to_313(monkeypatch, capsys) -> None:
    checker = _checker()
    monkeypatch.setattr(
        checker.importlib.metadata, "distributions", lambda: [_Distribution(">=3.11,<3.14")]
    )
    assert checker._get_direct_dep_names() >= {"matplotlib"}
    assert checker.REQUIRED_VERSIONS == [Version(value) for value in ("3.11.0", "3.12.0", "3.13.0")]
    assert checker.main() == 0
    assert "Python 3.11, 3.12, and 3.13" in capsys.readouterr().out


def test_checker_still_fails_for_unsupported_312(monkeypatch, capsys) -> None:
    checker = _checker()
    monkeypatch.setattr(
        checker.importlib.metadata, "distributions", lambda: [_Distribution(">=3.11,<3.12")]
    )
    assert checker._get_direct_dep_names() >= {"matplotlib"}
    assert checker.main() == 1
    assert "3.12.0" in capsys.readouterr().out
