"""tests/unit/test_oauth_cli.py

Tests for OAuth CLI UX fixes (R8-I) covering customer ID dash stripping,
Rich markup escaping, setup instructions, and inquirer removal.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.unit


class TestGoogleAdsCustomerIdDashStripping:
    """BUG-073: Customer IDs with dashes should be accepted and stripped."""

    def _run_ads_authenticate(self, tmp_path: Path, customer_id_input: str) -> MagicMock:
        """Run GoogleOAuthHelper.authenticate(service="ads") with mocked prompts.

        Prompt.ask sequence for ads flow:
          1. "Credentials file path" → /fake/creds.json
          2. "Developer Token ..." → "" (skip)
          3. "Customer ID ..." → customer_id_input
        """
        from dango.cli.oauth import GoogleOAuthHelper

        helper = GoogleOAuthHelper(tmp_path)
        helper.save_to_env = MagicMock()  # type: ignore[method-assign]

        # Create a fake credentials JSON file
        creds_file = tmp_path / "creds.json"
        creds_file.write_text(
            '{"type": "service_account", "client_email": "test@test.iam.gserviceaccount.com"}'
        )

        with (
            patch(
                "dango.cli.oauth.Prompt.ask",
                side_effect=[str(creds_file), "", customer_id_input],
            ),
            patch("dango.cli.oauth.Confirm.ask", return_value=False),
            patch("dango.cli.oauth.webbrowser"),
        ):
            helper.authenticate(service="ads")

        return helper.save_to_env

    def test_dashed_id_saved_without_dashes(self, tmp_path: Path) -> None:
        """Verify the actual GoogleOAuthHelper strips dashes before saving."""
        save_mock = self._run_ads_authenticate(tmp_path, "123-456-7890")

        customer_id_calls = [
            call for call in save_mock.call_args_list if call[0][0] == "GOOGLE_ADS_CUSTOMER_ID"
        ]
        assert len(customer_id_calls) == 1
        assert customer_id_calls[0][0][1] == "1234567890"  # dashes stripped

    def test_id_without_dashes_unchanged(self, tmp_path: Path) -> None:
        """Verify IDs without dashes pass through unchanged."""
        save_mock = self._run_ads_authenticate(tmp_path, "1234567890")

        customer_id_calls = [
            call for call in save_mock.call_args_list if call[0][0] == "GOOGLE_ADS_CUSTOMER_ID"
        ]
        assert len(customer_id_calls) == 1
        assert customer_id_calls[0][0][1] == "1234567890"

    def test_empty_customer_id_not_saved(self, tmp_path: Path) -> None:
        """Verify empty customer ID is not saved."""
        save_mock = self._run_ads_authenticate(tmp_path, "")

        customer_id_calls = [
            call for call in save_mock.call_args_list if call[0][0] == "GOOGLE_ADS_CUSTOMER_ID"
        ]
        assert len(customer_id_calls) == 0


class TestRichMarkupEscaping:
    """BUG-071: Consent URLs must be wrapped with escape() for Rich."""

    def test_all_consent_urls_use_escape(self) -> None:
        """Verify all 4 consent URL locations in the module use escape()."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        assert source.count("escape('https://console.cloud.google.com") >= 4

    def test_consent_urls_in_single_print_call(self) -> None:
        """Verify consent URLs are in single print() calls (not split across
        multiple calls where [dim] tags can break)."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        # The old pattern was 3 separate console.print() calls with [/dim]
        # in the URL line. The fix consolidates into one call.
        # Check there's no bare "See: https://console.cloud.google.com"
        # line that isn't inside an escape() call.
        import re

        bare_urls = re.findall(
            r"console\.print\([^)]*https://console\.cloud\.google\.com[^)]*\)",
            source,
        )
        for match in bare_urls:
            assert "escape(" in match, f"Unescaped consent URL found: {match[:80]}"


class TestOAuthSetupInstructions:
    """BUG-070/087: Setup instructions should mention both redirect URIs
    and explain port 8080 vs 8800. Tests inspect actual source code."""

    def test_source_contains_both_redirect_uris(self) -> None:
        """Verify both CLI and web UI redirect URIs appear in source."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        assert "http://localhost:8080/callback" in source
        assert "http://localhost:8800/api/oauth/callback" in source

    def test_source_explains_port_difference(self) -> None:
        """Verify the port 8080 vs 8800 explanation appears."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        assert "temporary server on port 8080" in source
        assert "separate from the Dango web UI on port 8800" in source

    def test_both_providers_have_web_uri(self) -> None:
        """Verify the web UI URI appears in both Google and Facebook sections."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        # Should appear at least twice (once per provider)
        assert source.count("http://localhost:8800/api/oauth/callback") >= 2

    def test_facebook_instructions_present(self) -> None:
        """Verify Facebook-specific instruction text exists."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        assert "Valid OAuth Redirect URIs" in source


class TestOAuthSetupNoInquirer:
    """BUG-069/091: oauth_setup should use click.prompt(), not inquirer.Text."""

    def test_no_inquirer_text_in_module(self) -> None:
        """Verify inquirer.Text is not used anywhere in the oauth commands module."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        assert "inquirer.Text" not in source

    def test_click_prompt_strips_whitespace(self) -> None:
        """Verify click.prompt result is stripped (prevents trailing space bugs)."""
        from dango.cli.commands import oauth as oauth_module

        source = inspect.getsource(oauth_module)
        assert "click.prompt(" in source
        assert ".strip()" in source
