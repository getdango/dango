"""tests/unit/test_source_wizard_spreadsheet_prompt.py

Unit tests for 1.0.8-AB: spreadsheet_url_or_id uses click.prompt() instead of inquirer.Text.
"""

from __future__ import annotations

from unittest.mock import patch

import click
import pytest


@pytest.mark.unit
class TestSpreadsheetUrlPromptUsesClick:
    """1.0.8-AB: inquirer 3.4.1's Text renderer redraws the full question+value
    line on every keystroke/paste event; its redraw math assumes each logical
    line occupies exactly one physical terminal row, so a pasted value longer
    than the terminal width (e.g. a full Google Sheets URL) wraps and the
    fixed 1-row "move up" leaves stale content on screen -- visually
    duplicating the line dozens of times. click.prompt() reads the whole
    input via the terminal's native line editing in a single blocking call,
    avoiding the bug entirely. Scoped to `spreadsheet_url_or_id` only."""

    def _param(self, default=None):
        return {
            "name": "spreadsheet_url_or_id",
            "type": "string",
            "prompt": "Spreadsheet ID or URL",
            "help": "Found in URL: docs.google.com/spreadsheets/d/SPREADSHEET_ID/edit",
            "default": default,
        }

    @patch("dango.cli.source_wizard.inquirer")
    @patch("dango.cli.source_wizard.click.prompt")
    def test_prompt_parameter_spreadsheet_url_uses_click_prompt(
        self, mock_click_prompt, mock_inquirer, tmp_path
    ):
        from dango.cli.source_wizard import SourceWizard

        long_url = (
            "https://docs.google.com/spreadsheets/d/1a2b3c4d5e6f7g8h9i0jklmnopqrstuvwxyz/edit#gid=0"
        )
        mock_click_prompt.return_value = long_url

        wizard = SourceWizard(tmp_path)
        metadata = {"display_name": "Google Sheets"}

        with patch("dango.cli.source_wizard.console"):
            value = wizard._prompt_parameter(
                self._param(), "my_sheets_source", "Google Sheets", metadata, required=True
            )

        assert value == long_url
        mock_click_prompt.assert_called_once()
        mock_inquirer.prompt.assert_not_called()

    @patch("dango.cli.source_wizard.inquirer")
    @patch("dango.cli.source_wizard.click.prompt")
    def test_prompt_parameter_spreadsheet_url_passes_default(
        self, mock_click_prompt, mock_inquirer, tmp_path
    ):
        from dango.cli.source_wizard import SourceWizard

        mock_click_prompt.return_value = "existing-default-id"

        wizard = SourceWizard(tmp_path)
        metadata = {"display_name": "Google Sheets"}

        with patch("dango.cli.source_wizard.console"):
            wizard._prompt_parameter(
                self._param(default="existing-default-id"),
                "my_sheets_source",
                "Google Sheets",
                metadata,
                required=True,
            )

        _, kwargs = mock_click_prompt.call_args
        assert kwargs.get("default") == "existing-default-id"

    @patch("dango.cli.source_wizard.inquirer")
    @patch("dango.cli.source_wizard.click.prompt")
    def test_prompt_parameter_spreadsheet_url_ctrl_c_returns_none(
        self, mock_click_prompt, mock_inquirer, tmp_path
    ):
        """click.prompt() raises click.Abort on Ctrl+C (unlike inquirer's
        `if not answers: return None` pattern). If left unguarded, that Abort
        would propagate past _collect_parameters' `if value is None: return
        None` check and past run()'s `except KeyboardInterrupt:` clause
        (Abort is a RuntimeError/Exception, not a KeyboardInterrupt), landing
        in the generic `except Exception as e:` handler and printing a bare
        "Error: " (str(Abort()) == '') before the wizard's normal "Source not
        added" / "Aborted!" cancellation message from commands/source.py --
        confirmed live via a real pty + real Ctrl+C keystroke. This branch
        must catch Abort locally and return None, exactly like every other
        field's `if not answers: return None`, so cancellation flows through
        the same silent path regardless of which field the user was on, and
        the wizard prints only "Source not added" / "Aborted!" with no
        spurious error line."""
        from dango.cli.source_wizard import SourceWizard

        mock_click_prompt.side_effect = click.Abort()

        wizard = SourceWizard(tmp_path)
        metadata = {"display_name": "Google Sheets"}

        with patch("dango.cli.source_wizard.console"):
            value = wizard._prompt_parameter(
                self._param(), "my_sheets_source", "Google Sheets", metadata, required=True
            )

        assert value is None
