"""tests/unit/test_source_wizard.py

Unit tests for source wizard UX bugs (P2-2, P2-4, P2-6, P2-7, P8-3).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from dango.ingestion.sources.registry import SOURCE_REGISTRY

# ---------------------------------------------------------------------------
# P2-2: Success message requires actual auth tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthSuccessRequiresTokens:
    """P2-2: Success message should only print when refresh/access token exists."""

    @patch("dango.cli.source_wizard.OAuthStorage")
    @patch("dango.cli.source_wizard.run_oauth_for_source")
    @patch("dango.cli.source_wizard.inquirer")
    def test_no_success_when_only_client_credentials(
        self, mock_inquirer, mock_run_oauth, mock_storage_cls, tmp_path
    ):
        """Credential with only client_id/secret (no tokens) should NOT show success."""
        from dango.cli.source_wizard import SourceWizard

        # Mock credential with client_id/secret but NO refresh_token
        mock_cred = MagicMock()
        mock_cred.is_expired.return_value = False
        mock_cred.credentials = {"client_id": "xxx", "client_secret": "yyy"}

        # First get() returns None (no existing creds), second returns mock_cred
        mock_storage_cls.return_value.get.side_effect = [None, mock_cred]
        mock_run_oauth.return_value = True

        # First inquirer prompt: choose "Set up OAuth now"
        # Second prompt (retry after warning): cancel
        mock_inquirer.prompt.side_effect = [
            {"oauth_action": "Set up OAuth now (recommended)"},
            None,  # Cancel at retry prompt
        ]
        mock_inquirer.List = MagicMock()

        wizard = SourceWizard(tmp_path)
        metadata = {"auth_type": "oauth", "display_name": "Google Ads"}

        with patch("dango.cli.source_wizard.console") as mock_console:
            printed = []
            mock_console.print = lambda *a, **kw: printed.append(str(a[0]) if a else "")

            wizard._handle_oauth_setup("google_ads", "my_ads", metadata)

        # Should NOT have printed success message
        success_msgs = [p for p in printed if "configured successfully" in p]
        assert len(success_msgs) == 0, f"Unexpected success message: {success_msgs}"

        # Should have printed the warning about client credentials
        warning_msgs = [p for p in printed if "authorization not completed" in p]
        assert len(warning_msgs) > 0, "Expected warning about incomplete authorization"

    @patch("dango.cli.source_wizard.OAuthStorage")
    @patch("dango.cli.source_wizard.run_oauth_for_source")
    @patch("dango.cli.source_wizard.inquirer")
    def test_success_when_refresh_token_present(
        self, mock_inquirer, mock_run_oauth, mock_storage_cls, tmp_path
    ):
        """Credential with refresh_token should show success."""
        from dango.cli.source_wizard import SourceWizard

        mock_cred = MagicMock()
        mock_cred.is_expired.return_value = False
        mock_cred.credentials = {
            "client_id": "xxx",
            "client_secret": "yyy",
            "refresh_token": "valid_token",
        }

        # First get() returns None (no existing creds), second returns mock_cred
        mock_storage_cls.return_value.get.side_effect = [None, mock_cred]
        mock_run_oauth.return_value = True

        mock_inquirer.prompt.return_value = {"oauth_action": "Set up OAuth now (recommended)"}
        mock_inquirer.List = MagicMock()

        wizard = SourceWizard(tmp_path)
        metadata = {"auth_type": "oauth", "display_name": "Google Ads"}

        with patch("dango.cli.source_wizard.console") as mock_console:
            printed = []
            mock_console.print = lambda *a, **kw: printed.append(str(a[0]) if a else "")

            result = wizard._handle_oauth_setup("google_ads", "my_ads", metadata)

        assert result is None  # Continue to params
        success_msgs = [p for p in printed if "configured successfully" in p]
        assert len(success_msgs) == 1


# ---------------------------------------------------------------------------
# P2-4/P2-6: Wizard exits on decline after OAuth skip
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOAuthSkipHandling:
    """P2-4/P2-6: 'Skip for now' returns 'skipped', main flow exits on decline."""

    @patch("dango.cli.source_wizard.OAuthStorage")
    @patch("dango.cli.source_wizard.inquirer")
    def test_skip_returns_skipped(self, mock_inquirer, mock_storage_cls, tmp_path):
        """'Skip for now' action should return 'skipped', not None."""
        from dango.cli.source_wizard import SourceWizard

        mock_storage_cls.return_value.get.return_value = None

        mock_inquirer.prompt.return_value = {
            "oauth_action": "Skip for now (configure manually later)"
        }
        mock_inquirer.List = MagicMock()

        wizard = SourceWizard(tmp_path)
        metadata = {"auth_type": "oauth", "display_name": "Google Ads"}

        with patch("dango.cli.source_wizard.console"):
            result = wizard._handle_oauth_setup("google_ads", "my_ads", metadata)

        assert result == "skipped"

    def test_wizard_exits_on_skip_decline(self, tmp_path):
        """When user declines 'Continue setup anyway?' after skip, wizard exits (returns False)
        without proceeding to parameter collection."""
        from dango.cli.source_wizard import SourceWizard

        wizard = SourceWizard(tmp_path)
        mock_collect = MagicMock()

        with (
            patch.object(wizard, "_handle_oauth_setup", return_value="skipped"),
            patch.object(wizard, "_select_source_flat", return_value="google_ads"),
            patch.object(wizard, "_show_source_info"),
            patch.object(wizard, "_get_source_name", return_value="my_ads"),
            patch.object(wizard, "_collect_parameters", mock_collect),
            patch("dango.cli.source_wizard.click.confirm", return_value=False),
            patch("dango.cli.source_wizard.console"),
            patch(
                "dango.cli.source_wizard.get_source_metadata",
                return_value={"auth_type": "oauth", "display_name": "Google Ads"},
            ),
        ):
            # click.Abort is caught by run()'s except Exception handler → returns False
            result = wizard.run()
            assert result is False
            # Crucially, parameter collection was never reached
            mock_collect.assert_not_called()

    def test_wizard_continues_on_skip_confirm(self, tmp_path):
        """When user confirms 'Continue setup anyway?' after skip, wizard proceeds to params."""
        from dango.cli.source_wizard import SourceWizard

        wizard = SourceWizard(tmp_path)

        with (
            patch.object(wizard, "_handle_oauth_setup", return_value="skipped"),
            patch.object(wizard, "_select_source_flat", return_value="google_ads"),
            patch.object(wizard, "_show_source_info"),
            patch.object(wizard, "_get_source_name", return_value="my_ads"),
            patch.object(wizard, "_collect_parameters", return_value=None),
            patch("dango.cli.source_wizard.click.confirm", return_value=True),
            patch("dango.cli.source_wizard.console"),
            patch(
                "dango.cli.source_wizard.get_source_metadata",
                return_value={
                    "auth_type": "oauth",
                    "display_name": "Google Ads",
                    "required_params": [],
                    "optional_params": [],
                },
            ),
        ):
            # Should proceed to params state (where _collect_parameters returns
            # None, ending the wizard — but crucially no click.Abort)
            result = wizard.run()
            assert result is False  # _collect_parameters returned None → cancelled


# ---------------------------------------------------------------------------
# P2-7: GA4 start_date uses date type
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGA4StartDate:
    """P2-7: GA4 start_date should use type 'date' with default_days_ago."""

    def test_ga4_start_date_uses_date_type(self):
        ga4 = SOURCE_REGISTRY["google_analytics"]
        start_date_param = next(p for p in ga4["optional_params"] if p["name"] == "start_date")
        assert start_date_param["type"] == "date"
        assert start_date_param["default_days_ago"] == 90
        assert "default" not in start_date_param


# ---------------------------------------------------------------------------
# P8-3: No "Credential setup required" without secrets_toml_template
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCredentialBlockGating:
    """P8-3: Sources without secrets_toml_template should not show credential block."""

    def test_no_credential_block_for_csv(self):
        """CSV has setup_guide but no secrets_toml_template — should not trigger credential block."""
        csv_meta = SOURCE_REGISTRY["csv"]
        assert csv_meta.get("setup_guide") is not None, "CSV should have setup_guide"
        assert csv_meta.get("secrets_toml_template") is None, (
            "CSV should not have secrets_toml_template"
        )

    def test_no_credential_block_for_local_files(self):
        """local_files has setup_guide but no secrets_toml_template."""
        lf_meta = SOURCE_REGISTRY["local_files"]
        assert lf_meta.get("setup_guide") is not None
        assert lf_meta.get("secrets_toml_template") is None

    def test_credential_block_for_salesforce(self):
        """Salesforce has both setup_guide and secrets_toml_template — should trigger."""
        sf_meta = SOURCE_REGISTRY["salesforce"]
        assert sf_meta.get("setup_guide") is not None
        assert sf_meta.get("secrets_toml_template") is not None
