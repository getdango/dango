"""tests/integration/test_stored_transform_error_reason.py

The `transform_error` a real `run_sync` stores for a failed dbt transform (30+ models downstream
of one failed model, real dbt + DuckDB, no Docker) starts with the reason, not with dbt's
boilerplate, in sync history and in every non-CLI consumer: the cloud initial sync, the sync
webhook and the MCP tool.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from dango.cli.commands import mcp_operations
from dango.config.helpers import load_config
from dango.ingestion import run_sync
from dango.platform.notifications.webhook import NotificationConfig, WebhookConfig
from dango.utils.post_sync import _send_sync_notification
from dango.web.routes.initial_sync import _sync_single_source
from tests.integration.dbt_outcome_support import make_csv_project, write_orders_csv

pytestmark = pytest.mark.integration

DOWNSTREAM_MODELS = 35
STORED_LIMIT = 500
REASON_START = "Failed models, seeds, snapshots or hooks (1): broken"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setenv("RUNTIME__DLTHUB_TELEMETRY", "false")
    monkeypatch.setenv("DBT_SEND_ANONYMOUS_USAGE_STATS", "false")
    monkeypatch.setenv("DANGO_TELEMETRY", "false")
    monkeypatch.delenv("DLT_DATA_DIR", raising=False)


def _sync(root: Path) -> dict[str, Any]:
    source = load_config(root).sources.get_source("shop")
    return run_sync(project_root=root, sources=[source])


@pytest.fixture
def failed_sync(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """A project whose second sync fails in dbt: one broken model with 35 models behind it."""
    root = make_csv_project(tmp_path / "proj", "id,name\n1,a\n2,b\n")
    assert _sync(root)["transform_status"] == "success"
    marts = root / "dbt" / "models" / "marts"
    (marts / "broken.sql").write_text(
        "select cast('abc' as integer) as x from {{ ref('stg_shop__shop') }}\n",
        encoding="utf-8",
        newline="\n",
    )
    for i in range(DOWNSTREAM_MODELS):
        (marts / f"child_{i:02d}.sql").write_text(
            "select * from {{ ref('broken') }}\n", encoding="utf-8", newline="\n"
        )
    write_orders_csv(root, "id,name\n1,a\n2,b\n3,c\n")  # new data: the source loads again
    return root, _sync(root)


def test_stored_error_leads_with_the_reason(failed_sync: tuple[Path, dict[str, Any]]) -> None:
    root, summary = failed_sync
    assert summary["transform_status"] == "failed"
    error = summary["transform_error"]
    assert len(error) <= STORED_LIMIT and "\n" not in error
    assert error.startswith(REASON_START)
    assert "Possible causes" not in error
    history = json.loads((root / ".dango" / "history" / "shop.json").read_text("utf-8"))
    assert history[-1]["transform_error"] == error


def test_initial_sync_raises_with_the_reason(failed_sync: tuple[Path, dict[str, Any]]) -> None:
    root, summary = failed_sync
    with (
        patch("dango.ingestion.run_sync", return_value=summary),
        pytest.raises(RuntimeError) as raised,
    ):
        asyncio.run(_sync_single_source(root, "shop"))
    assert str(raised.value) == summary["transform_error"]
    assert str(raised.value).startswith(REASON_START)


def test_sync_webhook_carries_the_reason(failed_sync: tuple[Path, dict[str, Any]]) -> None:
    root, summary = failed_sync
    config = NotificationConfig(
        webhooks=[WebhookConfig(name="hook", url="https://example.invalid/hook")]
    )
    with (
        patch("dango.platform.notifications.webhook.load_notification_config", return_value=config),
        patch("dango.utils.post_sync._deliver_to_webhooks") as deliver,
    ):
        _send_sync_notification(root, ["shop"], summary, trigger="cli")
    payload = deliver.call_args.args[1]
    assert payload.error == summary["transform_error"]
    assert payload.error.startswith(REASON_START)


def test_mcp_tool_reports_the_reason(
    failed_sync: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, summary = failed_sync
    monkeypatch.setattr(mcp_operations, "_get_project_root", lambda: root)
    monkeypatch.setattr("dango.ingestion.run_sync", lambda **kw: dict(summary))
    monkeypatch.setattr(
        "dango.visualization.metabase.refresh_metabase_connection", lambda r: (False, "", None)
    )
    result = mcp_operations.run_sync("shop")
    assert result["status"] == "failed"
    assert result["transform_error"] == summary["transform_error"]
    assert result["transform_error"].startswith(REASON_START)
