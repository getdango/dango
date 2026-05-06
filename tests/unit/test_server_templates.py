"""tests/unit/test_server_templates.py

Unit tests for ``build_systemd_unit()`` in
``dango/platform/cloud/_server_templates.py``.
"""

from __future__ import annotations

import pytest

from dango.platform.cloud._server_templates import SYSTEMD_UNIT, build_systemd_unit


@pytest.mark.unit
class TestBuildSystemdUnit:
    def test_default_matches_constant(self):
        """build_systemd_unit() with no args produces identical output to SYSTEMD_UNIT."""
        assert build_systemd_unit() == SYSTEMD_UNIT

    def test_workers_4_in_exec_start(self):
        """build_systemd_unit(workers=4) adds --workers 4 to ExecStart."""
        result = build_systemd_unit(workers=4)
        assert "ExecStart=/srv/dango/venv/bin/dango serve --workers 4" in result

    def test_workers_1_omits_flag(self):
        """build_systemd_unit(workers=1) omits --workers flag."""
        result = build_systemd_unit(workers=1)
        assert "--workers" not in result
        assert "ExecStart=/srv/dango/venv/bin/dango serve\n" in result

    def test_workers_none_omits_flag(self):
        """build_systemd_unit(workers=None) omits --workers flag."""
        result = build_systemd_unit(workers=None)
        assert "--workers" not in result

    def test_unit_structure(self):
        """Output contains expected systemd sections."""
        result = build_systemd_unit(workers=2)
        assert "[Unit]" in result
        assert "[Service]" in result
        assert "[Install]" in result
        assert "WantedBy=multi-user.target" in result
