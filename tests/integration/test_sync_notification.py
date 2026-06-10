"""tests/integration/test_sync_notification.py

Integration test: sync failure → webhook dispatch → Slack payload verified.
Tests the full notification pipeline from run_scheduled_sync through to
the HTTP POST payload, using real config loading and WebhookSender.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import dango.utils.dbt_lock  # noqa: F401 — force submodule into sys.modules

# dbt_lock module/function name collision workaround (see MEMORY.md)
_dbt_lock_module = sys.modules["dango.utils.dbt_lock"]

_JOBS_MOD = "dango.platform.scheduling.jobs"
_CFG_MOD = "dango.config.helpers"
_SYNC_PROC_MOD = "dango.platform.sync_process"
_HTTPX_CLIENT = "dango.platform.notifications.webhook.httpx.AsyncClient"
_ASYNC_SLEEP = "dango.platform.notifications.webhook.asyncio.sleep"


@pytest.mark.integration
class TestSyncFailureNotification:
    """End-to-end: sync failure dispatches Slack webhook with correct payload."""

    def test_sync_failure_dispatches_slack_webhook(self, tmp_path):
        # 1. Write schedules.yml with Slack-format webhook
        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        schedules_data = {
            "notifications": {
                "webhooks": [
                    {
                        "name": "slack-alerts",
                        "url": "https://hooks.slack.com/services/T00/B00/xxx",
                        "format": "slack",
                    },
                ],
                "on_failure": True,
            },
        }
        (dango_dir / "schedules.yml").write_text(yaml.safe_dump(schedules_data))

        # 2. Mock infrastructure: config, subprocess (fails)
        src = MagicMock()
        src.name = "src1"
        config = MagicMock()
        config.sources.get_source.side_effect = lambda n: src if n == "src1" else None

        # 3. Capture the coroutine from _notify's run_coroutine_threadsafe
        captured_coros = []

        def _capture_coro(coro, loop):
            captured_coros.append(coro)
            return MagicMock()

        # 4. Mock httpx to intercept the POST
        posted_payloads = []
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        async def _capture_post(url, json=None, **kwargs):
            posted_payloads.append(json)
            return mock_resp

        mock_client.post = _capture_post

        import dango.platform.scheduling.jobs as jobs_mod

        old_loop = jobs_mod._event_loop
        try:
            # Set a fake event loop so _notify doesn't skip
            jobs_mod._event_loop = MagicMock(spec=asyncio.AbstractEventLoop)

            with (
                patch(f"{_CFG_MOD}.load_config", return_value=config),
                patch(
                    f"{_SYNC_PROC_MOD}.launch_sync_subprocess",
                    return_value=(MagicMock(), "test_id", tmp_path / "fake.log"),
                ),
                patch(
                    f"{_SYNC_PROC_MOD}.poll_sync_status_blocking",
                    return_value=(False, {"error": "Connection refused", "phase": "failed"}),
                ),
                patch(f"{_JOBS_MOD}._broadcast"),
                patch(f"{_JOBS_MOD}.asyncio") as mock_asyncio_mod,
                patch(_HTTPX_CLIENT, return_value=mock_client),
                patch(_ASYNC_SLEEP, new_callable=AsyncMock),
            ):
                mock_asyncio_mod.run_coroutine_threadsafe = _capture_coro

                from dango.platform.scheduling.jobs import run_scheduled_sync

                run_scheduled_sync("daily", ["src1"], project_root=str(tmp_path))

                # 5. Run captured coroutines while httpx patch is still active
                assert len(captured_coros) == 1, "Expected exactly one notification coroutine"
                for coro in captured_coros:
                    asyncio.run(coro)
        finally:
            jobs_mod._event_loop = old_loop

        # 6. Verify Slack payload
        assert len(posted_payloads) == 1, "Expected exactly one POST"
        payload = posted_payloads[0]
        assert "attachments" in payload
        attachment = payload["attachments"][0]
        assert attachment["color"] == "#e01e5a"  # red for failure

        body_text = attachment["blocks"][1]["text"]["text"]
        assert "Connection refused" in body_text
        assert "daily" in body_text
