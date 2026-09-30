"""tests/unit/test_pii_masking.py

Tests for dango/governance/pii_masking.py: PII column-set resolution and result masking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.governance.pii_detector import _cache_findings
from dango.governance.pii_masking import MASK_VALUE, get_pii_column_names, mask_query_result
from dango.governance.pii_overrides import set_pii_override


def _finding(table: str, column: str) -> dict:
    return {
        "source": "s",
        "table_name": table,
        "column_name": column,
        "entity_type": "EMAIL_ADDRESS",
        "confidence": 0.9,
        "sample_count": 3,
        "scanned_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.mark.unit
class TestColumnSet:
    def test_column_set_from_findings_minus_not_pii_plus_pii(self, tmp_path: Path) -> None:
        _cache_findings(tmp_path, "s", "t", [_finding("t", "Email"), _finding("t", "name")])
        set_pii_override(tmp_path, "s", "t", "name", "not_pii", set_by="test")
        set_pii_override(tmp_path, "s", "t", "ssn", "pii", set_by="test")
        assert get_pii_column_names(tmp_path) == {"email", "ssn"}

    def test_column_set_empty_when_no_storage(self, tmp_path: Path) -> None:
        assert get_pii_column_names(tmp_path / "missing") == set()


@pytest.mark.unit
class TestMaskResult:
    def test_mask_masks_values_keeps_nulls_and_shape(self) -> None:
        result = {
            "columns": ["id", "email"],
            "rows": [[1, "fake-a@example.invalid"], [2, None]],
            "row_count": 2,
            "truncated": False,
        }
        out = mask_query_result(result, {"email"})
        assert out["rows"] == [[1, MASK_VALUE], [2, None]]
        assert out["columns"] == ["id", "email"]
        assert out["row_count"] == 2
        assert out["pii_masking"]["masked_columns"] == ["email"]
        assert result["rows"][0][1] != MASK_VALUE  # input not mutated

    def test_mask_case_insensitive_and_duplicate_columns(self) -> None:
        result = {"columns": ["EMAIL", "email", "id"], "rows": [["a", "b", 1]]}
        out = mask_query_result(result, {"email"})
        assert out["rows"] == [[MASK_VALUE, MASK_VALUE, 1]]
        assert out["pii_masking"]["masked_columns"] == ["EMAIL", "email"]

    def test_mask_note_when_no_pii_data(self) -> None:
        out = mask_query_result({"columns": ["a"], "rows": [[1]]}, set())
        assert "nothing was masked" in out["pii_masking"]["note"]
        assert out["pii_masking"]["masked_columns"] == []

    def test_mask_passes_error_results_through(self) -> None:
        err = {"error": "boom"}
        assert mask_query_result(err, {"email"}) == {"error": "boom"}
