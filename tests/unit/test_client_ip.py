"""tests/unit/test_client_ip.py

Tests for _get_client_ip() in web/routes/auth.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.mark.unit
class TestGetClientIp:
    def test_returns_none_when_client_is_none(self):
        from dango.web.routes.auth import _get_client_ip

        request = MagicMock()
        request.client = None
        assert _get_client_ip(request) is None

    def test_returns_host_when_client_present(self):
        from dango.web.routes.auth import _get_client_ip

        request = MagicMock()
        request.client.host = "192.168.1.1"
        assert _get_client_ip(request) == "192.168.1.1"
