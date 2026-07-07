"""tests/unit/test_scripts_helpers.py

Tests for script discovery, validation, and history helper functions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_HELPERS = "dango.web.routes.scripts_helpers"


def _write_script(scripts_dir: Path, name: str, content: str = "print('hello')") -> Path:
    """Create a script file and return its path."""
    scripts_dir.mkdir(parents=True, exist_ok=True)
    file_path = scripts_dir / name
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return file_path


# ---------------------------------------------------------------------------
# TestDiscoverScripts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscoverScripts:
    """Tests for _discover_scripts()."""

    def test_empty_dir(self, tmp_path: Path):
        """Returns empty list when no scripts exist."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        result = _discover_scripts(tmp_path)
        assert result == []

    def test_discovers_py_files(self, tmp_path: Path):
        """Discovers .py files in scripts/ directory."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "hello.py")
        _write_script(tmp_path / "scripts", "utils.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "hello.py" in names
        assert "utils.py" in names

    def test_skips_init_py(self, tmp_path: Path):
        """Skips __init__.py files."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "__init__.py")
        _write_script(tmp_path / "scripts", "main.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "__init__.py" not in names
        assert "main.py" in names

    def test_skips_dotfiles(self, tmp_path: Path):
        """Skips dot-prefixed files."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", ".hidden.py")
        _write_script(tmp_path / "scripts", "visible.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert ".hidden.py" not in names
        assert "visible.py" in names

    def test_skips_underscore_prefixed(self, tmp_path: Path):
        """Skips _-prefixed files and directories."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts", "_internal.py")
        _write_script(tmp_path / "scripts", "public.py")
        _write_script(tmp_path / "scripts" / "_private", "nested.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "_internal.py" not in names
        assert "public.py" in names
        assert "nested.py" not in names

    def test_recursive_discovery(self, tmp_path: Path):
        """Recursively discovers scripts in subdirectories."""
        from dango.web.routes.scripts_helpers import _discover_scripts

        _write_script(tmp_path / "scripts" / "marketing", "report.py")
        _write_script(tmp_path / "scripts" / "ops", "cleanup.py")

        result = _discover_scripts(tmp_path)
        names = [s["name"] for s in result]
        assert "marketing/report.py" in names
        assert "ops/cleanup.py" in names


# ---------------------------------------------------------------------------
# TestValidateScriptPath
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidateScriptPath:
    """Tests for _validate_script_path()."""

    def test_path_traversal_blocked(self, tmp_path: Path):
        """Path traversal outside scripts/ returns 400 JSONResponse."""
        from dango.web.routes.scripts_helpers import _validate_script_path

        result = _validate_script_path(tmp_path, "../outside.txt")
        assert hasattr(result, "status_code")
        assert result.status_code == 400  # type: ignore[union-attr]
        assert "DANGO-SC001" in result.body.decode()  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# TestSafeFilename
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSafeFilename:
    """Tests for _safe_filename()."""

    def test_replaces_slashes(self):
        """Replaces / and \\ with __."""
        from dango.web.routes.scripts_helpers import _safe_filename

        assert _safe_filename("marketing/report.py") == "marketing__report.py"
        assert _safe_filename("simple.py") == "simple.py"


# ---------------------------------------------------------------------------
# TestHistoryHelpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHistoryHelpers:
    """Tests for _append_history() and _load_history()."""

    def test_append_and_load(self, tmp_path: Path):
        """Append a history entry and load it back."""
        from dango.web.routes.scripts_helpers import _append_history, _load_history

        entry = {
            "run_id": "abc-123",
            "script_name": "hello.py",
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:05+00:00",
            "duration_seconds": 5.0,
            "status": "success",
            "exit_code": 0,
            "error": None,
        }
        _append_history(tmp_path, "hello.py", entry)

        result = _load_history(tmp_path, "hello.py", limit=50)
        assert len(result) == 1
        assert result[0]["run_id"] == "abc-123"
        assert result[0]["status"] == "success"

    def test_load_respects_limit(self, tmp_path: Path):
        """Only returns up to limit entries."""
        from dango.web.routes.scripts_helpers import _append_history, _load_history

        for i in range(10):
            _append_history(
                tmp_path,
                "hello.py",
                {
                    "run_id": f"run-{i}",
                    "script_name": "hello.py",
                    "started_at": f"2026-01-01T00:00:{i:02d}+00:00",
                    "finished_at": f"2026-01-01T00:00:{i + 5:02d}+00:00",
                    "duration_seconds": 5.0,
                    "status": "success",
                    "exit_code": 0,
                    "error": None,
                },
            )

        result = _load_history(tmp_path, "hello.py", limit=3)
        assert len(result) == 3

    def test_load_limit_zero_returns_all(self, tmp_path: Path):
        """limit=0 means no limit (for total counting)."""
        from dango.web.routes.scripts_helpers import _append_history, _load_history

        for i in range(5):
            _append_history(
                tmp_path,
                "hello.py",
                {
                    "run_id": f"run-{i}",
                    "script_name": "hello.py",
                    "started_at": f"2026-01-01T00:00:{i:02d}+00:00",
                    "finished_at": f"2026-01-01T00:00:{i + 5:02d}+00:00",
                    "duration_seconds": 5.0,
                    "status": "success",
                    "exit_code": 0,
                    "error": None,
                },
            )

        result = _load_history(tmp_path, "hello.py", limit=0)
        assert len(result) == 5

    def test_empty_history(self, tmp_path: Path):
        """Returns empty list for script with no history."""
        from dango.web.routes.scripts_helpers import _load_history

        result = _load_history(tmp_path, "nonexistent.py")
        assert result == []
