"""tests/unit/test_g3f1_old_version_summary.py

The upgrade summary names the real old version when the venv is read as data (untrusted venv).
"""

from __future__ import annotations

import pytest

from tests.unit.test_server_python_upgrade import _server, _upgrade

pytestmark = pytest.mark.unit


def test_the_summary_shows_the_old_version_when_the_venv_is_read_as_data() -> None:
    ssh, commands = _server(installed="1.0.13", untrusted=True)
    result = _upgrade(ssh, commands)
    assert result.old_version == "1.0.13"
    assert "rebuilt" in (result.venv_rebuild or "")
    first_run = next(i for i, c in enumerate(commands) if "pip install" in c or "-m venv" in c)
    assert not any(
        "import dango" in c for c in commands[:first_run]
    )  # never run before the rebuild
