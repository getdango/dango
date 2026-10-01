"""tests/unit/test_pii_min_match_count.py

Unit tests for the absolute minimum match count (ENTITY_MIN_MATCH_COUNT) in _scan_column.
Low-cardinality columns (enums, codes) have few DISTINCT samples, so one NER hit must not flag them.
"""

from unittest.mock import MagicMock, patch

import pytest

from dango.governance.pii_detector import ENTITY_MIN_MATCH_COUNT, _scan_column

_PII = "dango.governance.pii_detector"


def _scan(values: list[str], hits: int, entity: str = "PERSON") -> dict:
    r = MagicMock(entity_type=entity, score=0.85)
    analyzer = MagicMock()
    analyzer.analyze.side_effect = [[r]] * hits + [[] for _ in range(len(values) - hits)]
    with patch(f"{_PII}._get_analyzer", return_value=analyzer):
        return _scan_column(values, total_values=len(values))


@pytest.mark.unit
class TestEntityMinMatchCount:
    def test_person_single_hit_on_two_value_column_filtered(self) -> None:
        assert "PERSON" not in _scan(["eu", "us"], hits=1)

    def test_person_two_hits_filtered_even_at_high_ratio(self) -> None:
        assert "PERSON" not in _scan(["a", "b", "c"], hits=2)

    def test_person_three_hits_kept(self) -> None:
        assert "PERSON" in _scan(["a", "b", "c", "d", "e"], hits=3)

    def test_email_single_hit_still_kept(self) -> None:
        assert "EMAIL_ADDRESS" in _scan(["a", "b"], hits=1, entity="EMAIL_ADDRESS")

    def test_min_match_count_has_person(self) -> None:
        assert ENTITY_MIN_MATCH_COUNT["PERSON"] == 3
