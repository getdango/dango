"""tests/unit/test_bandit_baseline.py

Verify the Bandit baseline detects added occurrences and changed source.
"""

import pytest

from scripts.check_bandit_baseline import new_findings

pytestmark = pytest.mark.unit


def _finding(code: str, line: int = 10, filename: str = "dango/example.py") -> dict:
    return {
        "filename": filename,
        "test_id": "B608",
        "issue_severity": "MEDIUM",
        "issue_confidence": "MEDIUM",
        "issue_text": "Possible SQL injection vector through string-based query construction.",
        "code": f"{line}     {code}\n",
        "line_number": line,
    }


def test_existing_finding_survives_line_move() -> None:
    assert not new_findings([_finding("query(x)")], [_finding("query(x)", line=40)])


def test_second_same_finding_in_baselined_file_is_new() -> None:
    original = _finding("query(x)")
    assert new_findings([original], [original, _finding("query(x)", line=40)]) == [
        _finding("query(x)", line=40)
    ]


def test_changed_source_in_baselined_file_is_new() -> None:
    assert new_findings([_finding("query(x)")], [_finding("query(user_input)")]) == [
        _finding("query(user_input)")
    ]


def test_new_file_is_new() -> None:
    assert new_findings([_finding("query(x)")], [_finding("query(x)", filename="dango/new.py")])
