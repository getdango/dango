"""tests/unit/test_initial_sync_transform_failure.py

The cloud initial sync must not count a source as synced when its dbt transform failed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.web.routes.initial_sync import _sync_single_source

pytestmark = pytest.mark.unit


def _run(tmp_path: Path, summary: dict[str, Any]) -> None:
    with (
        patch("dango.config.helpers.load_config") as load,
        patch("dango.ingestion.run_sync", return_value=summary),
    ):
        load.return_value.sources.get_source.return_value = MagicMock()
        asyncio.run(_sync_single_source(tmp_path, "src1"))


def test_failed_transform_marks_the_source_failed(tmp_path: Path) -> None:
    summary = {"failed_sources": [], "transform_status": "failed", "transform_error": "dbt skipped"}
    with pytest.raises(RuntimeError, match="dbt skipped"):
        _run(tmp_path, summary)


def test_successful_and_skipped_transforms_pass(tmp_path: Path) -> None:
    _run(tmp_path, {"failed_sources": [], "transform_status": "success"})
    _run(tmp_path, {"failed_sources": [], "transform_status": "skipped"})
