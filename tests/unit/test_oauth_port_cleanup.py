"""tests/unit/test_oauth_port_cleanup.py

Tests that OAuth port is properly released on KeyboardInterrupt and timeout.
"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.unit
class TestOAuthPortCleanup:
    """Verify server.server_close() is called even on KeyboardInterrupt."""

    @patch("dango.oauth.webbrowser")
    @patch("dango.oauth.time")
    @patch("dango.oauth._ReusableTCPServer")
    def test_keyboard_interrupt_closes_server(self, mock_server_cls, mock_time, mock_wb):
        """When KeyboardInterrupt occurs during the wait loop, server_close is called."""
        from dango.oauth import OAuthManager

        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        # Make time.sleep raise KeyboardInterrupt on first call
        mock_time.sleep.side_effect = KeyboardInterrupt
        mock_time.time.return_value = 0

        manager = MagicMock(spec=OAuthManager)
        manager.callback_port = 8080
        manager.callback_url = "http://localhost:8080/callback"

        with pytest.raises(KeyboardInterrupt):
            OAuthManager.start_oauth_flow(manager, "test", "http://example.com/auth")

        mock_server.server_close.assert_called()

    @patch("dango.oauth.webbrowser")
    @patch("dango.oauth.time")
    @patch("dango.oauth._ReusableTCPServer")
    def test_timeout_calls_server_close_and_join(self, mock_server_cls, mock_time, mock_wb):
        """On timeout, both server_close and thread join are called."""
        from dango.oauth import OAuthCallbackHandler, OAuthManager

        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server

        # Simulate time progressing past timeout
        mock_time.time.side_effect = [0, 0, 200]
        mock_time.sleep.return_value = None

        # Reset handler state
        OAuthCallbackHandler.oauth_response = None
        OAuthCallbackHandler.oauth_error = None

        manager = MagicMock(spec=OAuthManager)
        manager.callback_port = 8080
        manager.callback_url = "http://localhost:8080/callback"

        # Mock inquirer to return Cancel (inquirer is imported locally inside start_oauth_flow)
        with patch.dict("sys.modules", {"inquirer": MagicMock()}) as _:
            import sys

            sys.modules["inquirer"].prompt.return_value = {"timeout_action": "Cancel"}
            sys.modules["inquirer"].List = MagicMock()
            result = OAuthManager.start_oauth_flow(
                manager, "test", "http://example.com/auth", timeout_seconds=60
            )

        assert result is None
        # server_close called both in the timeout path AND in finally
        assert mock_server.server_close.call_count >= 2
