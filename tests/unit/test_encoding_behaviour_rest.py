"""tests/unit/test_encoding_behaviour_rest.py

Platform, notebooks, web and visualization writers use UTF-8 and LF under a non-UTF-8 locale.
"""

from pathlib import Path
from typing import Any

import pytest

from tests.factories.cp1252_child import run_child

pytestmark = pytest.mark.unit


def _assert_utf8_lf(result: dict[str, Any]) -> None:
    opens = [o for o in result["opens"] if any(c in o["mode"] for c in "wax")]
    assert opens, "no text write was observed"
    assert {o["encoding"] for o in result["opens"]} == {"utf-8"}, result["opens"]
    assert {o["newline"] for o in opens} == {"\n"}, opens


def test_docker_compose_render_is_utf8_with_lf(tmp_path: Path) -> None:
    result = run_child(
        """
        from dango.config.models import DangoConfig
        from dango.platform.docker import render_docker_compose
        cfg = DangoConfig.model_validate({"project": {"name": "Caf\\u00e9 Desk", "created_by": "t", "purpose": "t"}})
        _opens.clear()
        render_docker_compose(root, cfg)
        raw = (root / "docker-compose.yml").read_bytes()
        result = {"opens": _opens, "crlf": b"\\r\\n" in raw, "utf8": raw.decode("utf-8") != ""}
        """,
        tmp_path,
    )
    _assert_utf8_lf(result)
    assert not result["crlf"]


def test_network_routing_roundtrip_is_utf8_with_lf(tmp_path: Path) -> None:
    result = run_child(
        """
        from dango.platform.local.network import NetworkConfig
        net = NetworkConfig()
        _opens.clear()
        net.save_routing({"version": "1.0", "projects": {TEXT: {"port": 1}}})
        loaded = net.load_routing()
        result = {"opens": _opens, "same": TEXT in loaded["projects"]}
        """,
        tmp_path,
    )
    _assert_utf8_lf(result)
    assert result["same"]


def test_marimo_runner_record_is_utf8_with_lf(tmp_path: Path) -> None:
    result = run_child(
        """
        from dango.notebooks import marimo_process
        pid_file = root / "nb.pid"
        _opens.clear()
        marimo_process.write_runner_record(pid_file, 1)
        result = {"opens": _opens, "runner": marimo_process.is_runner_record(pid_file)}
        """,
        tmp_path,
    )
    _assert_utf8_lf(result)
    assert result["runner"]


def test_initial_sync_state_roundtrip_is_utf8_with_lf(tmp_path: Path) -> None:
    result = run_child(
        """
        from dango.web.routes import initial_sync
        initial_sync._state_path = lambda: root / ".dango" / "state" / "i.json"
        initial_sync._sync_state.current_source_name = TEXT
        _opens.clear()
        initial_sync._save_state()
        initial_sync._load_state()
        result = {"opens": _opens}
        """,
        tmp_path,
    )
    _assert_utf8_lf(result)


def test_dashboard_sync_state_is_utf8_with_lf(tmp_path: Path) -> None:
    result = run_child(
        """
        from dango.visualization.dashboard_manager import DashboardManager
        mgr = DashboardManager(root, "http://localhost:3000", session_token="t")
        _opens.clear()
        mgr._update_sync_state()
        result = {"opens": _opens}
        """,
        tmp_path,
    )
    _assert_utf8_lf(result)
