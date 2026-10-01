"""tests/unit/test_source_setup_wizard_parity.py

Parity: the interactive wizard and the non-interactive service save identical sources.yml entries.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.cli.source_wizard import SourceWizard
from dango.config.helpers import load_config, save_config
from dango.ingestion.sources.setup_service import create_source


def _default_answers(questions, **_kwargs):
    """Answer every inquirer question with its default (empty string when None)."""
    return {q.name: (q.default if q.default is not None else "") for q in questions}


@pytest.mark.unit
def test_wizard_local_files_defaults_match_create_source(tmp_path: Path, sample_config):
    wizard_root = tmp_path / "wizard"
    service_root = tmp_path / "service"
    for root in (wizard_root, service_root):
        root.mkdir()
        save_config(sample_config, root)

    wizard = SourceWizard(wizard_root)
    with (
        patch.object(wizard, "_select_source_flat", return_value="local_files"),
        patch.object(wizard, "_get_source_name", return_value="orders"),
        patch.object(wizard, "_print_git_warnings"),
        patch("dango.cli.source_wizard.inquirer.prompt", side_effect=_default_answers),
        patch("dango.cli.source_wizard.Confirm.ask", return_value=False),
        patch("dango.cli.source_wizard.console"),
    ):
        assert wizard.run() is True

    create_source(service_root, "local_files", "orders", {})

    wizard_source = load_config(wizard_root).sources.get_source("orders")
    service_source = load_config(service_root).sources.get_source("orders")
    assert wizard_source is not None and service_source is not None
    assert wizard_source.model_dump(mode="json") == service_source.model_dump(mode="json")
    assert (wizard_root / "data" / "uploads" / "orders").is_dir()
