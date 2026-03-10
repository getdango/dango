"""tests/unit/test_notebook_proxy.py

Tests for dango.notebooks.proxy — HTTP and WebSocket proxy utilities.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.unit
class TestBuildMarimoUrl:
    def test_basic_url(self):
        from dango.notebooks.proxy import _build_marimo_url

        assert _build_marimo_url(7805, "/api/health") == "http://127.0.0.1:7805/api/health"

    def test_url_with_query(self):
        from dango.notebooks.proxy import _build_marimo_url

        assert (
            _build_marimo_url(7805, "/api/data", "page=1")
            == "http://127.0.0.1:7805/api/data?page=1"
        )


@pytest.mark.unit
class TestEnsureMarimoRunning:
    @patch("dango.notebooks.manager.get_marimo_status")
    def test_returns_port_if_running(self, mock_status):
        mock_status.return_value = {"running": True, "port": 7805}

        from dango.notebooks.proxy import _ensure_marimo_running

        assert _ensure_marimo_running("/fake") == 7805

    @patch("dango.notebooks.manager.start_marimo")
    @patch("dango.notebooks.manager.get_marimo_status")
    def test_starts_if_not_running(self, mock_status, mock_start):
        mock_status.side_effect = [
            {"running": False, "port": None},
            {"running": True, "port": 7805},
        ]

        from dango.notebooks.proxy import _ensure_marimo_running

        assert _ensure_marimo_running("/fake") == 7805
        mock_start.assert_called_once()

    @patch("dango.notebooks.manager.start_marimo")
    @patch("dango.notebooks.manager.get_marimo_status")
    def test_raises_if_start_fails(self, mock_status, mock_start):
        mock_status.return_value = {"running": False, "port": None}

        from dango.notebooks.proxy import _ensure_marimo_running

        with pytest.raises(RuntimeError, match="Marimo failed to start"):
            _ensure_marimo_running("/fake")


@pytest.mark.unit
class TestProxyToMarimo:
    @patch("dango.notebooks.proxy.httpx.AsyncClient")
    def test_proxy_success(self, mock_client_cls):
        import asyncio

        mock_response = MagicMock()
        mock_response.content = b"hello"
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html"}

        mock_client = AsyncMock()
        mock_client.request.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.query = ""
        mock_request.headers = {"accept": "text/html"}

        from dango.notebooks.proxy import proxy_to_marimo

        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(proxy_to_marimo(mock_request, "/test", 7805))
        finally:
            loop.close()

        assert response.status_code == 200
        assert response.body == b"hello"

    @patch("dango.notebooks.proxy.httpx.AsyncClient")
    def test_proxy_returns_502_on_failure(self, mock_client_cls):
        import asyncio

        mock_client = AsyncMock()
        mock_client.request.side_effect = Exception("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.query = ""
        mock_request.headers = {}

        from dango.notebooks.proxy import proxy_to_marimo

        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(proxy_to_marimo(mock_request, "/test", 7805))
        finally:
            loop.close()

        assert response.status_code == 502
