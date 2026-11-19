"""tests/unit/test_env_helpers_cancel_message.py

Cancelling source setup at the credential check says only what is true about what was saved.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.cli.env_helpers import handle_validation_result

pytestmark = pytest.mark.unit


def test_cancel_does_not_claim_the_source_was_saved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("API_KEY=\n", encoding="utf-8")

    with patch("inquirer.prompt", return_value={"action": "Cancel source setup"}):
        assert handle_validation_result(False, ["API_KEY"], env_file, "src") is False

    out = " ".join(capsys.readouterr().out.split())
    assert "Source setup cancelled" in out
    assert "was saved" not in out
    assert "not added to .dango/sources.yml" in out
    assert not (tmp_path / ".dango" / "sources.yml").exists()
