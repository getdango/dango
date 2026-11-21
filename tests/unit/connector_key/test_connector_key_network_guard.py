"""tests/unit/connector_key/test_connector_key_network_guard.py

The connector key-contract tests run with the network blocked: a connection or lookup attempt must fail the test
with the target address, while loopback stays usable.
"""

from __future__ import annotations

import socket

import pytest


@pytest.mark.unit
class TestConnectorKeyNetworkGuard:
    def test_a_lookup_is_blocked_and_recorded_with_its_target(self, network_attempts):
        with pytest.raises(OSError, match="blocked.example.invalid"):
            socket.getaddrinfo("blocked.example.invalid", 443)
        assert network_attempts == ["resolve blocked.example.invalid"]
        network_attempts.clear()  # the guard fails any test that leaves an attempt behind

    def test_a_connection_to_an_address_is_blocked_and_recorded(self, network_attempts):
        with pytest.raises(OSError, match=r"203\.0\.113\.7:80"):
            socket.create_connection(("203.0.113.7", 80), timeout=0.2)
        assert "connect 203.0.113.7:80" in network_attempts
        network_attempts.clear()

    def test_loopback_still_works(self, network_attempts):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=2):
                pass
        assert network_attempts == []
