"""tests/unit/test_port_manager.py

Tests for dango.cli.helpers.port_manager — port checking utilities.
"""

import socket
from unittest.mock import MagicMock, patch

import psutil
import pytest

from dango.cli.helpers.port_manager import check_port_in_use, get_process_using_port


@pytest.mark.unit
class TestCheckPortInUse:
    @patch("dango.cli.helpers.port_manager.socket.socket")
    def test_bind_succeeds_port_available(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_port_in_use(8080) is False
        mock_sock.bind.assert_called_once_with(("0.0.0.0", 8080))

    @patch("dango.cli.helpers.port_manager.socket.socket")
    def test_bind_raises_oserror_port_in_use(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_sock.bind.side_effect = OSError("Address already in use")
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

        assert check_port_in_use(8080) is True

    @patch("dango.cli.helpers.port_manager.socket.socket")
    def test_so_reuseaddr_set(self, mock_socket_cls):
        mock_sock = MagicMock()
        mock_socket_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
        mock_socket_cls.return_value.__exit__ = MagicMock(return_value=False)

        check_port_in_use(8080)
        mock_sock.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)


@pytest.mark.unit
class TestGetProcessUsingPort:
    @patch("dango.cli.helpers.port_manager.psutil")
    def test_process_listening_on_target_port(self, mock_psutil):
        conn = MagicMock()
        conn.laddr.port = 8080
        conn.status = "LISTEN"
        conn.pid = 1234
        mock_psutil.net_connections.return_value = [conn]

        assert get_process_using_port(8080) == 1234

    @patch("dango.cli.helpers.port_manager.psutil")
    def test_no_connections_returns_none(self, mock_psutil):
        mock_psutil.net_connections.return_value = []
        assert get_process_using_port(8080) is None

    @patch("dango.cli.helpers.port_manager.psutil")
    def test_connections_on_different_port(self, mock_psutil):
        conn = MagicMock()
        conn.laddr.port = 3000
        conn.status = "LISTEN"
        conn.pid = 5678
        mock_psutil.net_connections.return_value = [conn]

        assert get_process_using_port(8080) is None

    @patch("dango.cli.helpers.port_manager.psutil")
    def test_right_port_but_established_not_listen(self, mock_psutil):
        conn = MagicMock()
        conn.laddr.port = 8080
        conn.status = "ESTABLISHED"
        conn.pid = 1234
        mock_psutil.net_connections.return_value = [conn]

        assert get_process_using_port(8080) is None

    @patch("dango.cli.helpers.port_manager.psutil")
    def test_access_denied_returns_none(self, mock_psutil):
        mock_psutil.AccessDenied = psutil.AccessDenied
        mock_psutil.net_connections.side_effect = psutil.AccessDenied(0)

        assert get_process_using_port(8080) is None

    @patch("dango.cli.helpers.port_manager.psutil")
    def test_attribute_error_returns_none(self, mock_psutil):
        """Covers conn.laddr being None on some platforms."""
        mock_psutil.AccessDenied = psutil.AccessDenied
        conn = MagicMock()
        conn.laddr = None  # None.port raises AttributeError
        mock_psutil.net_connections.return_value = [conn]

        assert get_process_using_port(8080) is None
