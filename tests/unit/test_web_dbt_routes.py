"""tests/unit/test_web_dbt_routes.py

Tests for dango/web/routes/dbt.py's background dbt-run task.
"""

import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestRunDbtModelTaskMetabaseRefresh:
    @pytest.mark.anyio
    async def test_metabase_refresh_does_not_block_event_loop(self, tmp_path: Path) -> None:
        """1.0.8-Q15 regression test: refresh_metabase_connection() must run via
        asyncio.to_thread(), not block the event loop directly.

        Proven by a real deadlock-if-broken setup: the mocked refresh blocks (in its
        own thread, if properly wrapped) on a threading.Event that only a
        concurrently-scheduled coroutine can set. Enforcement is wall-clock elapsed
        time, not asyncio.wait_for: if the refresh call is NOT properly threaded, it
        blocks the event loop synchronously from inside a single Task step, which
        means asyncio.wait_for's own timeout callback can never get a chance to fire
        either (the loop is fully occupied by the same blocking call) -- wait_for
        can't preempt the very bug it would be trying to catch, so it would let the
        broken case run slow and still pass. Measuring elapsed time after the fact
        avoids that race entirely: broken code takes ~= blocking_refresh's internal
        timeout (bounded, so this test can never hang forever); fixed code completes
        almost instantly because the concurrent coroutine gets to run immediately on
        the now-free event loop and releases the worker thread right away.
        """
        from dango.web.routes.dbt import run_dbt_model_task

        release_event = threading.Event()
        concurrent_ran = asyncio.Event()

        def blocking_refresh(project_root: Path) -> tuple[bool, str | None, str | None]:
            # Runs in a worker thread if properly wrapped in asyncio.to_thread --
            # blocks until the concurrent coroutine below releases it. Bounded at 2s
            # so the test can never hang forever even in the broken case.
            release_event.wait(timeout=2)
            return (True, None, None)

        async def concurrent_probe() -> None:
            # A real yield back to the event loop. If run_dbt_model_task's refresh call
            # is blocking the loop directly, this coroutine never gets scheduled until
            # after that blocking call finally returns (up to blocking_refresh's own 2s
            # internal timeout) -- the elapsed-time assertion below is what actually
            # catches that, not this coroutine running late.
            await asyncio.sleep(0)
            concurrent_ran.set()
            release_event.set()

        with (
            patch("dango.web.routes.dbt.get_project_root", return_value=tmp_path),
            patch("dango.web.routes.dbt.subprocess.run") as mock_subprocess_run,
            patch("dango.web.routes.dbt.ws_manager.broadcast", new_callable=AsyncMock),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.utils.dbt_status.update_model_status"),
            patch(
                "dango.visualization.metabase.refresh_metabase_connection",
                side_effect=blocking_refresh,
            ),
        ):
            mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            start = time.monotonic()
            await asyncio.gather(
                run_dbt_model_task("test_model", cascade=False),
                concurrent_probe(),
            )
            elapsed = time.monotonic() - start

        assert concurrent_ran.is_set()
        assert elapsed < 1.0, (
            f"run_dbt_model_task took {elapsed:.2f}s -- refresh_metabase_connection() "
            "appears to be blocking the event loop directly instead of running via "
            "asyncio.to_thread()"
        )
