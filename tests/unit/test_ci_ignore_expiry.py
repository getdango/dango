"""tests/unit/test_ci_ignore_expiry.py

Tests for scripts/check_audit_ignores.py: the dated pip-audit ignore list must fail
once an entry has expired, is undated, or is dated too far out.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_audit_ignores.py"
IGNORES = REPO_ROOT / ".github" / "pip-audit-ignores.txt"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_audit_ignores", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_script()


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "ignores.txt"
    path.write_text(text, encoding="utf-8")
    return path


TODAY = date(2026, 10, 17)


def test_valid_entries_pass(script, tmp_path):
    path = _write(tmp_path, "CVE-2026-1  # reason, expires 2026-12-01\n")
    assert script.main([str(path)], today=TODAY) == 0


def test_expired_entry_fails(script, tmp_path, capsys):
    path = _write(tmp_path, "CVE-2026-1  # reason, expires 2026-10-16\n")
    assert script.main([str(path)], today=TODAY) != 0
    assert "CVE-2026-1" in capsys.readouterr().err


def test_entry_valid_through_its_expiry_date(script, tmp_path):
    path = _write(tmp_path, "CVE-2026-1  # reason, expires 2026-10-17\n")
    assert script.main([str(path)], today=TODAY) == 0


def test_one_expired_among_valid_fails(script, tmp_path):
    path = _write(
        tmp_path,
        "CVE-2026-1  # ok, expires 2026-12-01\nCVE-2026-2  # old, expires 2026-01-01\n",
    )
    assert script.main([str(path)], today=TODAY) != 0


def test_entry_without_date_fails(script, tmp_path):
    path = _write(tmp_path, "CVE-2026-1  # no date here\n")
    assert script.main([str(path)], today=TODAY) != 0


def test_entry_too_far_out_fails(script, tmp_path):
    path = _write(tmp_path, "CVE-2026-1  # reason, expires 2027-12-31\n")
    assert script.main([str(path)], today=TODAY) != 0


def test_comments_and_blank_lines_are_ignored(script, tmp_path):
    path = _write(tmp_path, "# header\n\n   \nCVE-2026-1  # reason, expires 2026-11-01\n")
    assert script.main([str(path)], today=TODAY) == 0


def test_empty_list_passes(script, tmp_path):
    path = _write(tmp_path, "# nothing accepted\n")
    assert script.main([str(path)], today=TODAY) == 0


def test_missing_file_fails(script, tmp_path):
    assert script.main([str(tmp_path / "absent.txt")], today=TODAY) != 0


def test_checked_in_ignore_list_parses():
    """The real list must be well formed (expiry itself is enforced by CI on the date)."""
    script = _load_script()
    entries = script.parse_entries(IGNORES.read_text(encoding="utf-8"))
    assert all(vuln_id and isinstance(expires, date) for vuln_id, expires in entries)
