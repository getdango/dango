"""tests/unit/test_r8e_bugfixes.py

Unit tests for R8-E bug fixes (BUG-054, BUG-083, BUG-107).
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# BUG-083: Session idle timeout changed to 7 days
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdleTimeoutDefault:
    def test_default_idle_timeout_is_7_days(self) -> None:
        """DEFAULT_IDLE_TIMEOUT_MINUTES should be 10080 (7 days)."""
        from dango.auth.sessions import DEFAULT_IDLE_TIMEOUT_MINUTES

        assert DEFAULT_IDLE_TIMEOUT_MINUTES == 10080


# ---------------------------------------------------------------------------
# BUG-054: Login page redirects authenticated users
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoginRedirect:
    @staticmethod
    def _run_coro(coro: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_login_redirects_authenticated_user(self) -> None:
        """login_page() redirects to / when a valid session cookie exists."""
        mock_user = MagicMock()
        mock_user.id = "user-1"

        mock_request = MagicMock()
        mock_request.cookies = {"dango_session": "fake-valid-token"}
        mock_request.app.state.project_root = "/tmp/test-project"

        with (
            patch("dango.auth.admin.is_auth_enabled", return_value=True),
            patch("dango.auth.admin.get_auth_db_path", return_value="/tmp/.dango/auth.db"),
            patch("dango.auth.sessions.validate_session", return_value=mock_user),
        ):
            from dango.web.routes.ui import login_page

            response = self._run_coro(login_page(mock_request))
            assert response.status_code == 302
            assert response.headers["location"] == "/"

    def test_login_renders_when_no_session(self) -> None:
        """login_page() renders login page when no session cookie is present."""
        mock_request = MagicMock()
        mock_request.cookies = {}
        mock_request.app.state.project_root = "/tmp/test-project"
        mock_request.state.user = None

        with (
            patch("dango.auth.admin.is_auth_enabled", return_value=True),
            patch("dango.web.routes.ui._render_template") as mock_render,
        ):
            mock_render.return_value = MagicMock(status_code=200)

            from dango.web.routes.ui import login_page

            response = self._run_coro(login_page(mock_request))
            assert response.status_code == 200
            mock_render.assert_called_once()


# ---------------------------------------------------------------------------
# BUG-107: Sync endpoint uses asyncio.create_task
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSyncAsyncio:
    def test_trigger_sync_uses_create_task(self) -> None:
        """trigger_sync should use asyncio.create_task, not BackgroundTasks."""
        # Verify that BackgroundTasks is not imported in the module
        import inspect

        from dango.web.routes import sync as sync_module

        source = inspect.getsource(sync_module.trigger_sync)
        assert "background_tasks" not in source
        assert "create_task" in source

    def test_trigger_manual_sync_uses_create_task(self) -> None:
        """trigger_manual_sync should use asyncio.create_task, not BackgroundTasks."""
        import inspect

        from dango.web.routes import sync as sync_module

        source = inspect.getsource(sync_module.trigger_manual_sync)
        assert "background_tasks" not in source
        assert "create_task" in source

    def test_no_background_tasks_import(self) -> None:
        """sync.py should not import BackgroundTasks at all."""
        from dango.web.routes import sync as sync_module

        assert not hasattr(sync_module, "BackgroundTasks")
