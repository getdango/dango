"""tests/unit/test_source_wizard_empty_sync_policy.py

1.0.10-S9: `_save_source()` empty-sync-policy prompt for replace-mode-capable
source types, gated on `REPLACE_MODE_SOURCE_TYPES`. Split from
test_source_wizard.py (already at the 500-line pre-commit limit) per
STANDARDS.md §7 split-at-creation rule.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

_KEEP_CHOICE = "Keep the existing data, fail the sync, and alert you (recommended)"
_REPLACE_CHOICE = "Replace the existing data — the table becomes empty too"


def _wizard_and_config(tmp_path):
    """Shared setup: a SourceWizard plus a mocked config with a real sources list
    (so appended DataSource objects can be inspected after _save_source())."""
    from dango.cli.source_wizard import SourceWizard

    mock_config = MagicMock()
    mock_config.sources.sources = []
    return SourceWizard(tmp_path), mock_config


@pytest.mark.unit
class TestEmptySyncPolicyPrompt:
    """`_save_source()` asks a one-time empty-sync-policy question for
    replace-mode-capable source types only, gated on `REPLACE_MODE_SOURCE_TYPES`."""

    @patch("dango.cli.source_wizard.save_config")
    @patch("dango.cli.source_wizard.load_config")
    @patch("dango.cli.source_wizard.inquirer")
    def test_save_source_prompts_for_replace_mode_type(
        self, mock_inquirer, mock_load_config, mock_save_config, tmp_path
    ):
        """Choosing 'Replace' for a CSV source sets empty_sync_policy='allow'."""
        wizard, mock_config = _wizard_and_config(tmp_path)
        mock_load_config.return_value = mock_config
        mock_inquirer.prompt.return_value = {"empty_sync_choice": _REPLACE_CHOICE}
        mock_inquirer.List = MagicMock()

        source_config = {"name": "my_csv", "type": "csv", "csv": {"directory": "data"}}
        wizard._save_source(source_config)

        mock_inquirer.prompt.assert_called_once()
        assert mock_config.sources.sources[0].empty_sync_policy == "allow"
        mock_save_config.assert_called_once()

    @pytest.mark.parametrize("answer", [None, _KEEP_CHOICE])
    @patch("dango.cli.source_wizard.save_config")
    @patch("dango.cli.source_wizard.load_config")
    @patch("dango.cli.source_wizard.inquirer")
    def test_save_source_defaults_to_block_for_replace_mode_type(
        self, mock_inquirer, mock_load_config, _mock_save_config, answer, tmp_path
    ):
        """Choosing 'Keep' (or Ctrl-C, simulated by `inquirer.prompt` returning
        None) for a CSV source sets empty_sync_policy='block'."""
        wizard, mock_config = _wizard_and_config(tmp_path)
        mock_load_config.return_value = mock_config
        mock_inquirer.prompt.return_value = {"empty_sync_choice": answer} if answer else None
        mock_inquirer.List = MagicMock()

        source_config = {"name": "my_csv", "type": "csv", "csv": {"directory": "data"}}
        wizard._save_source(source_config)

        assert mock_config.sources.sources[0].empty_sync_policy == "block"

    @patch("dango.cli.source_wizard.save_config")
    @patch("dango.cli.source_wizard.load_config")
    @patch("dango.cli.source_wizard.inquirer")
    def test_save_source_skips_prompt_for_merge_only_type(
        self, mock_inquirer, mock_load_config, mock_save_config, tmp_path
    ):
        """freshdesk is merge-only (dango/ingestion/dlt_sources/freshdesk/__init__.py
        never sets write_disposition="replace") and is not in
        REPLACE_MODE_SOURCE_TYPES, so the prompt must never fire and the
        DataSource field default ('block') applies untouched."""
        wizard, mock_config = _wizard_and_config(tmp_path)
        mock_load_config.return_value = mock_config

        source_config = {"name": "my_freshdesk", "type": "freshdesk", "generic_config": {}}
        wizard._save_source(source_config)

        mock_inquirer.prompt.assert_not_called()
        assert "empty_sync_policy" not in source_config
        assert mock_config.sources.sources[0].empty_sync_policy == "block"
        mock_save_config.assert_called_once()
