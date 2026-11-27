"""tests/unit/test_run_sync_dlt_partial_error.py

WP-67: the failure list names the failed resources of a dlt "partial" result instead of "Unknown error".
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from dango.ingestion.dlt_runner import DltPipelineRunner, run_sync
from tests.factories.partial_file_source import make_source

pytestmark = pytest.mark.unit


def _run(tmp_path: Path, result: dict) -> tuple[dict, str]:
    (tmp_path / "files").mkdir()
    source = make_source("csv", tmp_path / "files", "crm")
    buf = StringIO()
    with (
        patch("dango.ingestion.dlt_runner.console", Console(file=buf, width=2000)),
        patch.object(DltPipelineRunner, "run_source", return_value=result),
    ):
        summary = run_sync(tmp_path, [source], skip_dbt=True)
    return summary, buf.getvalue()


def test_a_partial_result_names_its_failed_resources(tmp_path: Path) -> None:
    result = {
        "status": "partial",
        "source": "crm",
        "failed_resources": ["tickets", "[bold]deals[/bold]"],
        "succeeded_resources": ["users"],
    }

    summary, printed = _run(tmp_path, result)

    error = summary["failed_sources"][0]["error"]
    assert "tickets" in error and "deals" in error
    assert "Unknown error" not in error and "Unknown error" not in printed
    assert "[bold]deals[/bold]" in printed  # printed literally, not as markup


def test_a_result_with_an_error_text_keeps_it(tmp_path: Path) -> None:
    summary, _ = _run(tmp_path, {"status": "failed", "source": "crm", "error": "boom"})
    assert summary["failed_sources"][0]["error"] == "boom"


def test_a_failure_with_no_text_still_says_unknown_error(tmp_path: Path) -> None:
    summary, _ = _run(tmp_path, {"status": "failed", "source": "crm"})
    assert summary["failed_sources"][0]["error"] == "Unknown error"
