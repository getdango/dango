"""tests/unit/test_rest_api_config.py

Tests for REST API source configuration building in DltPipelineRunner._build_rest_api_config.
Covers: endpoint fields, paginator mapping (BUG-080), custom_header auth (BUG-088),
and ${VAR} env var resolution in headers (BUG-079).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# _build_rest_api_config — endpoint fields (data_selector, params, headers)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildRestApiConfig:
    """Tests for DltPipelineRunner._build_rest_api_config with new fields."""

    def _build(self, source_kwargs: dict) -> dict:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        return runner._build_rest_api_config(source_kwargs)

    def test_backward_compat_simple(self) -> None:
        """Old-format endpoints (path + name only) still work."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users"}],
            }
        )
        resources = result["config"]["resources"]
        assert len(resources) == 1
        assert resources[0]["name"] == "users"
        assert resources[0]["endpoint"] == {"path": "/users"}
        assert resources[0]["primary_key"] == "id"

    def test_data_selector_passthrough(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users", "data_selector": "data.items"}],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["data_selector"] == "data.items"

    def test_params_passthrough(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users", "params": {"limit": "100"}}],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["params"] == {"limit": "100"}

    def test_headers_applied_to_client(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "headers": {"User-Agent": "Dango/1.0"},
                "endpoints": [{"path": "/users", "name": "users"}],
            }
        )
        assert result["config"]["client"]["headers"] == {"User-Agent": "Dango/1.0"}

    def test_primary_key_override(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users", "primary_key": "user_id"}],
            }
        )
        assert result["config"]["resources"][0]["primary_key"] == "user_id"

    def test_empty_data_selector_omitted(self) -> None:
        """Empty string data_selector is not included (falsy check)."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users", "data_selector": ""}],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert "data_selector" not in ep


# ---------------------------------------------------------------------------
# _build_rest_api_config — paginator support (BUG-080)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildRestApiConfigPaginator:
    """Tests for paginator config mapping to dlt format."""

    def _build(self, source_kwargs: dict) -> dict:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        return runner._build_rest_api_config(source_kwargs)

    def test_no_paginator_backward_compat(self) -> None:
        """Endpoints without paginator key still work (backward compat)."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users"}],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert "paginator" not in ep

    def test_auto_paginator_omitted(self) -> None:
        """Auto paginator type is not passed to dlt (let dlt auto-detect)."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users", "paginator": {"type": "auto"}}],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert "paginator" not in ep

    def test_header_link_paginator(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [
                    {"path": "/users", "name": "users", "paginator": {"type": "header_link"}}
                ],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["paginator"] == "header_link"

    def test_page_number_paginator_with_param(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [
                    {
                        "path": "/users",
                        "name": "users",
                        "paginator": {"type": "page_number", "page_param": "p"},
                    }
                ],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["paginator"] == {"type": "page_number", "page_param": "p"}

    def test_cursor_paginator(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [
                    {
                        "path": "/users",
                        "name": "users",
                        "paginator": {"type": "cursor", "cursor_path": "meta.next"},
                    }
                ],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["paginator"] == {"type": "cursor", "cursor_path": "meta.next"}

    def test_offset_paginator(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [
                    {
                        "path": "/users",
                        "name": "users",
                        "paginator": {"type": "offset", "limit": 50},
                    }
                ],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["paginator"] == {"type": "offset", "limit": 50}

    def test_none_paginator_maps_to_single_page(self) -> None:
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "endpoints": [{"path": "/users", "name": "users", "paginator": {"type": "none"}}],
            }
        )
        ep = result["config"]["resources"][0]["endpoint"]
        assert ep["paginator"] == "single_page"


# ---------------------------------------------------------------------------
# _build_rest_api_config — custom_header auth (BUG-088)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildRestApiConfigCustomHeader:
    """Tests for custom_header auth type mapping."""

    def _build(self, source_kwargs: dict) -> dict:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        return runner._build_rest_api_config(source_kwargs)

    def test_custom_header_auth_as_header(self) -> None:
        """Custom header auth adds token as a client header."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "auth_type": "custom_header",
                "auth_token": "secret123",
                "auth_header_name": "X-Shopify-Access-Token",
                "endpoints": [{"path": "/orders", "name": "orders"}],
            }
        )
        client = result["config"]["client"]
        assert client["headers"]["X-Shopify-Access-Token"] == "secret123"
        assert "auth" not in client

    def test_custom_header_merged_with_existing_headers(self) -> None:
        """Custom header auth merges with explicit headers."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "auth_type": "custom_header",
                "auth_token": "tok",
                "auth_header_name": "X-Token",
                "headers": {"User-Agent": "Dango/1.0"},
                "endpoints": [{"path": "/data", "name": "data"}],
            }
        )
        hdrs = result["config"]["client"]["headers"]
        assert hdrs["User-Agent"] == "Dango/1.0"
        assert hdrs["X-Token"] == "tok"

    def test_custom_header_no_token_graceful(self) -> None:
        """Custom header with no token does not add auth header."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "auth_type": "custom_header",
                "auth_token": None,
                "auth_header_name": "X-Token",
                "endpoints": [{"path": "/data", "name": "data"}],
            }
        )
        client = result["config"]["client"]
        assert "headers" not in client
        assert "auth" not in client


# ---------------------------------------------------------------------------
# _build_rest_api_config — ${VAR} env var resolution in headers (BUG-079)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildRestApiConfigEnvHeaders:
    """Tests for ${VAR} env var resolution in custom headers."""

    def _build(self, source_kwargs: dict) -> dict:
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        return runner._build_rest_api_config(source_kwargs)

    def test_env_var_header_resolved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """${VAR} syntax is resolved from os.environ."""
        monkeypatch.setenv("MY_SECRET_TOKEN", "resolved_value")
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "headers": {"Authorization": "${MY_SECRET_TOKEN}"},
                "endpoints": [{"path": "/data", "name": "data"}],
            }
        )
        assert result["config"]["client"]["headers"]["Authorization"] == "resolved_value"

    def test_literal_header_unchanged(self) -> None:
        """Non-${...} header values are passed through as-is."""
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "headers": {"User-Agent": "Dango/1.0"},
                "endpoints": [{"path": "/data", "name": "data"}],
            }
        )
        assert result["config"]["client"]["headers"]["User-Agent"] == "Dango/1.0"

    def test_missing_env_var_kept_as_literal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Unresolved ${VAR} falls back to the literal ${VAR} string."""
        monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
        result = self._build(
            {
                "base_url": "https://api.example.com",
                "headers": {"X-Token": "${NONEXISTENT_VAR}"},
                "endpoints": [{"path": "/data", "name": "data"}],
            }
        )
        assert result["config"]["client"]["headers"]["X-Token"] == "${NONEXISTENT_VAR}"
