"""tests/unit/test_sync_transform_status_callers.py

A sync whose data loaded but whose dbt transform failed is a failed sync for the MCP tool and
the sync webhook, and `dango sync` prints dbt model names literally (no Rich markup).
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console

from dango.cli.commands import mcp_operations
from dango.platform.notifications.webhook import EventType, NotificationConfig, WebhookConfig
from dango.utils.post_sync import _send_sync_notification

pytestmark = pytest.mark.unit

LOADED = {"failed_count": 0, "success_count": 1, "failed_sources": [], "results": []}
TRANSFORM_FAILED = {**LOADED, "transform_status": "failed", "transform_error": "dbt skipped 2"}


@pytest.fixture
def mcp_project(tmp_path: Path, sample_config: Any, monkeypatch: pytest.MonkeyPatch) -> Path:
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_operations, "_get_project_root", lambda: tmp_path)
    return tmp_path


def _mcp_status(monkeypatch: pytest.MonkeyPatch, summary: dict[str, Any]) -> dict[str, Any]:
    monkeypatch.setattr("dango.ingestion.run_sync", lambda **kw: dict(summary))
    monkeypatch.setattr(
        "dango.visualization.metabase.refresh_metabase_connection", lambda root: (False, "", None)
    )
    return mcp_operations.run_sync("test_source")


def test_mcp_run_sync_reports_failed_when_the_transform_failed(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _mcp_status(monkeypatch, TRANSFORM_FAILED)
    assert result["status"] == "failed"
    assert result["transform_error"] == "dbt skipped 2"


def test_mcp_run_sync_status_is_otherwise_unchanged(
    mcp_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _mcp_status(monkeypatch, {**LOADED, "transform_status": "success"})["status"] == (
        "completed"
    )
    interrupted = {**TRANSFORM_FAILED, "interrupted": True, "failed_count": 1}
    assert _mcp_status(monkeypatch, interrupted)["status"] == "interrupted"


def _sent_payload(summary: dict[str, Any], tmp_path: Path) -> Any:
    config = NotificationConfig(webhooks=[WebhookConfig(name="w", url="https://example.com/hook")])
    sent: list[Any] = []
    with (
        patch("dango.platform.notifications.webhook.load_notification_config", return_value=config),
        patch("dango.utils.post_sync._deliver_to_webhooks", lambda hooks, p: sent.append(p)),
    ):
        _send_sync_notification(tmp_path, ["s"], summary, trigger="cli")
    return sent[0] if sent else None


def test_a_failed_transform_sends_the_failure_event_not_completed(tmp_path: Path) -> None:
    payload = _sent_payload(TRANSFORM_FAILED, tmp_path)
    assert payload.event_type == EventType.SYNC_FAILED
    assert payload.error == "dbt skipped 2"


def test_failed_sources_and_a_failed_transform_are_both_named(tmp_path: Path) -> None:
    summary = {**TRANSFORM_FAILED, "failed_count": 1, "failed_sources": [{"name": "bad"}]}
    payload = _sent_payload(summary, tmp_path)
    assert payload.event_type == EventType.SYNC_FAILED
    assert payload.error == "Failed sources: bad; dbt skipped 2"


def test_a_clean_sync_still_sends_completed(tmp_path: Path) -> None:
    payload = _sent_payload({**LOADED, "transform_status": "success"}, tmp_path)
    assert payload.event_type == EventType.SYNC_COMPLETED
    assert payload.error is None


def test_display_dbt_output_prints_model_names_literally() -> None:
    from dango.ingestion import dlt_runner

    buffer = StringIO()
    out = "1 of 1 OK created sql table model marts.broken[red]mart ........ [OK in 0.10s]"
    with patch.object(dlt_runner, "console", Console(file=buffer, width=200, force_terminal=False)):
        dlt_runner._display_dbt_output(out)
    assert "marts.broken[red]mart" in buffer.getvalue()
