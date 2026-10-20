"""tests/unit/test_compose_rest_legacy.py

With the compose-helper marker absent, the Metabase refresh/readiness checks, the service
health probe, the H2 size probe and the admin repair issue exactly the direct docker calls
they always did (argv, env, cwd, timeouts), and never call ``sudo``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform import compose_exec as ce
from dango.platform.common import metabase_admin_repair as repair
from dango.utils import db_health
from dango.visualization import metabase as mb
from dango.web import helpers

pytestmark = pytest.mark.unit

SINCE = "2026-10-20T12:34:56.123456+00:00"


@pytest.fixture(autouse=True)
def no_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ce, "MARKER", str(tmp_path / "absent"))


def calls(mock: MagicMock) -> list[tuple[list[str], dict[str, Any]]]:
    return [(c.args[0], c.kwargs) for c in mock.call_args_list]


def test_log_ready_polls_docker_logs() -> None:
    done = MagicMock(stdout="Metabase Initialization COMPLETE", stderr="")
    with patch.object(mb.subprocess, "run", return_value=done) as run:
        assert mb._wait_for_metabase_log_ready("c-1", SINCE, max_wait_seconds=5) is True
    assert calls(run) == [
        (
            ["docker", "logs", "--since", SINCE, "c-1"],
            {"capture_output": True, "text": True, "timeout": 5},
        )
    ]


def test_refresh_uses_docker_ps_then_docker_restart(tmp_path: Path) -> None:
    root = tmp_path / "p"
    (root / ".dango").mkdir(parents=True)
    name = "dango-test-metabase-1"
    ran = [MagicMock(stdout=f"{name}\n"), MagicMock(returncode=0, stderr="")]
    with (
        patch("dango.platform.docker.get_compose_project_name", return_value="dango-test"),
        patch.object(mb.subprocess, "run", side_effect=ran) as run,
        patch.object(mb, "_wait_for_metabase_log_ready", return_value=True) as ready,
        patch.object(mb, "load_metabase_admin_credentials", return_value=None),
        patch.object(mb, "_apply_metabase_site_url_catchup"),
    ):
        result = mb.refresh_metabase_connection(root)
    assert result == (True, None, None)
    got = calls(run)
    assert got[0] == (
        ["docker", "ps", "--filter", f"name={name}", "--format", "{{.Names}}"],
        {"capture_output": True, "text": True, "timeout": 5},
    )
    assert got[1] == (
        ["docker", "restart", name],
        {"capture_output": True, "text": True, "timeout": 30},
    )
    assert ready.call_args.args[0] == name


def test_service_check_uses_docker_ps() -> None:
    with patch.object(helpers.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="Up 3 hours")
        assert helpers.check_service_via_docker("metabase") == "running"
    assert calls(run) == [
        (
            ["docker", "ps", "--filter", "name=metabase", "--format", "{{.Status}}"],
            {"capture_output": True, "text": True, "timeout": 10},
        )
    ]


def test_h2_size_runs_compose_exec_in_the_project(tmp_path: Path) -> None:
    db_health._component_disk_cache = None
    db_health._component_disk_cache_time = 0
    with patch.object(db_health.subprocess, "run") as run:
        run.return_value = MagicMock(returncode=0, stdout="2097152\n")
        assert db_health.get_component_disk_usage(tmp_path)["metabase"]["size_mb"] == 2.0
    assert calls(run) == [
        (
            ["docker", "compose", "exec", "-T", "metabase"]
            + ["stat", "-c", "%s", "/metabase-data/metabase.db.mv.db"],
            {"capture_output": True, "text": True, "timeout": 5, "cwd": tmp_path},
        )
    ]


def test_admin_repair_docker_calls_keep_argv_env_and_timeouts(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    env = {"COMPOSE_PROJECT_NAME": "dango-test"}
    recorded = MagicMock(returncode=0, stdout="OK [[[tok]]]", stderr="")
    with patch.object(repair.subprocess, "run", return_value=recorded) as run:
        repair._compose(root, env, "stop", "metabase", timeout=90)
        repair._compose(root, env, "up", "-d", "metabase", timeout=120)
        assert repair._run_reset_cli(root, env, "a@b.co") == "tok"
        assert repair._metabase_running(env) is True
    got = [(argv, kw) for argv, kw in calls(run) if argv[:3] != ["docker", "compose", "version"]]
    assert got[0] == (
        ["docker", "compose", "stop", "metabase"],
        {"cwd": root, "env": env, "capture_output": True, "timeout": 90, "check": False},
    )
    assert got[1][0] == ["docker", "compose", "up", "-d", "metabase"]
    assert got[1][1]["timeout"] == 120
    assert got[2][0] == ["docker", "compose", "run", "--rm", "--no-deps", "-T", "metabase"] + [
        "reset-password",
        "a@b.co",
    ]
    assert got[2][1]["timeout"] == 300 and got[2][1]["env"] is env
    assert got[3] == (
        ["docker", "ps", "-q"]
        + ["--filter", "label=com.docker.compose.project=dango-test"]
        + ["--filter", "label=com.docker.compose.service=metabase"],
        {"capture_output": True, "text": True, "timeout": 30, "check": False},
    )


def test_admin_repair_still_requires_a_local_compose_file(tmp_path: Path) -> None:
    root = tmp_path / "p"
    root.mkdir()
    meta = {"metabase_url": "http://localhost:3000", "admin": {"email": "a@b.co"}}
    with patch.object(repair, "load_metabase_metadata", return_value=meta):
        assert repair.repair_admin_credential(root) == {
            "status": "skipped",
            "reason": "no_local_compose",
        }
