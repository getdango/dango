"""tests/unit/test_lock_classification.py

``_analyze_error`` treats exactly DuckDB's real lock messages as lock errors before the status-code
branches; a file-in-use text from a CSV or Excel file keeps the class it had before.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.factories.duckdb_holder import make_warehouse, real_lock_error

pytestmark = pytest.mark.unit


@pytest.fixture
def analyze_error() -> Any:
    from dango.ingestion.dlt_runner import DltPipelineRunner

    return object.__new__(DltPipelineRunner)._analyze_error


class TestFileInUseIsNotAWarehouseLock:
    @pytest.mark.parametrize(
        "text",
        [
            (
                "Failed to read file schema from C:\\p\\sales.csv: IO Error: Cannot open file "
                '"C:\\p\\sales.csv": The process cannot access the file because it is being used '
                "by another process."
            ),
            (
                "Failed to read file schema from /p/sales.csv: IO Error: Cannot open file "
                '"/p/sales.csv": The process cannot access the file because it is being used '
                "by another process."
            ),
        ],
    )
    def test_csv_open_in_another_program_is_classified_as_before(
        self, analyze_error: Any, text: str
    ) -> None:
        result = analyze_error(Exception(text), "my_csv")
        assert "Data Validation Error" in result
        assert "DuckDB Lock Error" not in result

    def test_file_in_use_without_a_duckdb_file_name_is_not_the_early_lock_class(
        self, analyze_error: Any
    ) -> None:
        text = (
            'Cannot open file "C:\\p\\data.xlsx": The process cannot access the file because it '
            "is being used by another process. HTTP 429"
        )
        assert "Rate Limit Error" in analyze_error(Exception(text), "my_csv")


class TestRealLockMessages:
    @pytest.mark.parametrize("read_only", [False, True])
    @pytest.mark.parametrize("code", ["401", "403", "429"])
    def test_real_message_with_the_code_in_pid_and_path(
        self, analyze_error: Any, tmp_path: Path, code: str, read_only: bool
    ) -> None:
        err, pid = real_lock_error(
            make_warehouse(tmp_path / f"pytest-{code}1" / "warehouse.duckdb"), read_only=read_only
        )
        text = str(err).replace(f"(PID {pid})", f"(PID 7{code}5)")
        assert f"7{code}5" in text or "PID" not in text
        result = analyze_error(Exception(text), "my_source")
        assert "DuckDB Lock Error" in result
        assert "Rate Limit Error" not in result
        assert "Authentication Error" not in result

    def test_real_message_wrapped_by_dlt_with_a_real_429_is_a_lock_error(
        self, analyze_error: Any, tmp_path: Path
    ) -> None:
        err, _pid = real_lock_error(make_warehouse(tmp_path / "w" / "warehouse.duckdb"))
        text = f"Step load failed: {err} (the api also said HTTP 429 Too Many Requests)"
        assert "DuckDB Lock Error" in analyze_error(Exception(text), "my_source")
