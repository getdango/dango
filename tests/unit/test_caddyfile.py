"""tests/unit/test_caddyfile.py

Unit tests for Caddyfile template generation
(dango/platform/cloud/_server_templates.py).
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
class TestBuildCaddyfile:
    """Tests for build_caddyfile() function."""

    def test_http_mode_listens_port_80(self):
        """No domain → Caddyfile listens on :80."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile()
        assert result.startswith(":80 {")

    def test_http_mode_reverse_proxy(self):
        """HTTP mode proxies to localhost:8800."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile()
        assert "reverse_proxy localhost:8800" in result

    def test_http_mode_has_security_headers(self):
        """HTTP mode includes X-Content-Type-Options and friends."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile()
        assert "X-Content-Type-Options nosniff" in result
        assert "X-Frame-Options SAMEORIGIN" in result
        assert "Referrer-Policy strict-origin-when-cross-origin" in result
        assert "interest-cohort=()" in result

    def test_http_mode_no_hsts(self):
        """HTTP mode must NOT include HSTS (can't enforce on HTTP)."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile()
        assert "Strict-Transport-Security" not in result

    def test_https_mode_uses_domain(self):
        """Domain provided → Caddyfile uses domain as address."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile("app.example.com")
        assert result.startswith("app.example.com {")

    def test_https_mode_reverse_proxy(self):
        """HTTPS mode still proxies to localhost:8800."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile("app.example.com")
        assert "reverse_proxy localhost:8800" in result

    def test_https_mode_has_hsts(self):
        """HTTPS mode includes Strict-Transport-Security header."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile("app.example.com")
        assert "Strict-Transport-Security" in result
        assert "max-age=63072000" in result

    def test_https_mode_has_security_headers(self):
        """HTTPS mode includes all security headers."""
        from dango.platform.cloud._server_templates import build_caddyfile

        result = build_caddyfile("app.example.com")
        assert "X-Content-Type-Options nosniff" in result
        assert "X-Frame-Options SAMEORIGIN" in result
        assert "Referrer-Policy strict-origin-when-cross-origin" in result

    def test_backward_compat_alias(self):
        """CADDYFILE constant equals build_caddyfile() with no args."""
        from dango.platform.cloud._server_templates import CADDYFILE, build_caddyfile

        assert CADDYFILE == build_caddyfile()

    def test_none_domain_same_as_no_args(self):
        """Passing domain=None produces the same output as no args."""
        from dango.platform.cloud._server_templates import build_caddyfile

        assert build_caddyfile(None) == build_caddyfile()

    def test_output_ends_with_newline(self):
        """Output ends with a newline for clean file writing."""
        from dango.platform.cloud._server_templates import build_caddyfile

        assert build_caddyfile().endswith("\n")
        assert build_caddyfile("example.com").endswith("\n")
