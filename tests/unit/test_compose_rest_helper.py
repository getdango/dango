"""tests/unit/test_compose_rest_helper.py

With the compose-helper marker present, the Metabase refresh/readiness checks, the service
health probe, the H2 size probe and the admin repair reach Docker only through
``sudo -n dango-compose <verb>`` (never ``docker``), and every caller timeout exceeds the
helper's lock wait plus the verb budget.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform import compose_exec as ce
from dango.platform.cloud import compose_helper as ch
from dango.platform.common import metabase_admin_repair as repair
from dango.utils import db_health
from dango.visualization import metabase as mb
from dango.web import helpers

pytestmark = pytest.mark.unit

FAKE_TOOL = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/$(basename "$0").calls"
cat >/dev/null
if [ "$(basename "$0")" = sudo ] && [ -f "$d/out.$3" ]; then cat "$d/out.$3"; fi
exit "$(cat "$d/rc.$3" 2>/dev/null || echo 0)"
"""
RUNNING = '{"Service":"metabase","State":"running"}\n{"Service":"dbt-docs","State":"running"}\n'
EMAIL = "admin@example.com"
TOKEN = "sentinel-reset-token"


@pytest.fixture
def shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "shim"
    d.mkdir()
    for name in ("sudo", "docker", "docker-compose"):
        (d / name).write_text(FAKE_TOOL)
        (d / name).chmod(0o755)
    marker = tmp_path / "active"
    marker.write_text("")
    marker.chmod(0o644)
    monkeypatch.setattr(ce, "MARKER", str(marker))
    monkeypatch.setattr(ce, "_ROOT_UID", os.getuid())
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


@pytest.fixture
def timeouts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    seen: list[tuple[str, float]] = []
    real = ce._helper_run

    def spy(verb: str, args: Any, timeout: float, text: bool) -> Any:
        seen.append((verb, timeout))
        return real(verb, args, timeout, text)

    monkeypatch.setattr(ce, "_helper_run", spy)
    return seen


def verbs(d: Path) -> list[str]:
    path = d / "sudo.calls"
    lines = path.read_text().splitlines() if path.exists() else []
    return [" ".join(line.split()[2:]) for line in lines]


def docker_calls(d: Path) -> list[str]:
    out: list[str] = []
    for name in ("docker.calls", "docker-compose.calls"):
        path = d / name
        out += path.read_text().splitlines() if path.exists() else []
    return out


def assert_timeouts_cover_budget(seen: list[tuple[str, float]]) -> None:
    assert seen
    for verb, timeout in seen:
        wait = ch.SUDO_LOCK_WAIT if verb in ch.LOCKED_VERBS else 0
        assert timeout > wait + ch.VERB_BUDGET[verb], verb


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    return root


@pytest.fixture
def refresh_env() -> Iterator[None]:
    with (
        patch.object(mb, "load_metabase_admin_credentials", return_value=None),
        patch.object(mb, "_apply_metabase_site_url_catchup"),
        patch.object(mb.time, "sleep"),
    ):
        yield


class TestRefreshMetabaseConnection:
    def test_ps_restart_logs_only(
        self, shim: Path, project: Path, timeouts: list[tuple[str, float]], refresh_env: None
    ) -> None:
        (shim / "out.ps").write_text(RUNNING)
        (shim / "out.logs").write_text("metabase-1  | Metabase Initialization COMPLETE in 1.0s\n")

        ok, error, _session = mb.refresh_metabase_connection(project)

        assert (ok, error) == (True, None)
        got = verbs(shim)
        assert got[0] == "ps" and got[1] == "restart metabase"
        assert re.fullmatch(r"logs metabase --since [0-9T:.+Z-]{1,40}", got[2])
        assert docker_calls(shim) == []
        assert_timeouts_cover_budget(timeouts)

    def test_not_running_is_reported_without_restart(
        self, shim: Path, project: Path, refresh_env: None
    ) -> None:
        (shim / "out.ps").write_text('{"Service":"metabase","State":"exited"}\n')

        result = mb.refresh_metabase_connection(project)

        assert result == (False, "Metabase container not running", None)
        assert verbs(shim) == ["ps"]
        assert docker_calls(shim) == []

    def test_busy_restart_is_a_plain_failure(
        self, shim: Path, project: Path, refresh_env: None
    ) -> None:
        (shim / "out.ps").write_text(RUNNING)
        (shim / "rc.restart").write_text("75")

        ok, error, session = mb.refresh_metabase_connection(project)

        assert ok is False and session is None
        assert error is not None and "Traceback" not in error
        assert verbs(shim) == ["ps", "restart metabase"]


class TestLogReadiness:
    def test_logs_verb_with_since(
        self, shim: Path, project: Path, timeouts: list[tuple[str, float]]
    ) -> None:
        (shim / "out.logs").write_text("Metabase Initialization COMPLETE\n")
        since = "2026-10-20T12:34:56.123456+00:00"

        ready = mb._wait_for_metabase_log_ready(
            "dango-x-metabase-1", since, max_wait_seconds=5, project_root=project
        )

        assert ready is True
        assert verbs(shim) == [f"logs metabase --since {since}"]
        assert docker_calls(shim) == []
        assert_timeouts_cover_budget(timeouts)

    def test_busy_and_missing_line_are_not_ready(self, shim: Path, project: Path) -> None:
        (shim / "rc.logs").write_text("75")

        with patch.object(mb.time, "sleep"):
            ready = mb._wait_for_metabase_log_ready(
                "c", "2026-10-20T00:00:00+00:00", max_wait_seconds=0, project_root=project
            )

        assert ready is False


class TestServiceHealth:
    def test_no_sudo_and_no_docker_per_poll(self, shim: Path) -> None:
        with patch.object(helpers, "check_service_via_http", return_value="running") as http:
            assert helpers.check_service_via_docker("metabase") == "running"
        http.assert_called_once_with("metabase")
        assert verbs(shim) == [] and docker_calls(shim) == []


class TestH2Size:
    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        db_health._component_disk_cache = None
        db_health._component_disk_cache_time = 0

    def test_h2_size_verb(
        self, shim: Path, tmp_path: Path, timeouts: list[tuple[str, float]]
    ) -> None:
        (shim / "out.h2-size").write_text("5242880\n")

        result = db_health.get_component_disk_usage(tmp_path)

        assert result["metabase"]["size_mb"] == 5.0
        assert verbs(shim) == ["h2-size"]
        assert docker_calls(shim) == []
        assert_timeouts_cover_budget(timeouts)

    def test_busy_or_failed_leaves_size_empty(self, shim: Path, tmp_path: Path) -> None:
        (shim / "rc.h2-size").write_text("75")

        assert db_health.get_component_disk_usage(tmp_path)["metabase"]["size_mb"] is None


@pytest.fixture
def repair_env(shim: Path, project: Path) -> Iterator[None]:
    store = MagicMock()
    store.load_pending.return_value = None
    with (
        patch.object(repair, "load_metabase_metadata") as meta,
        patch.object(repair, "MetabaseCredentialStore", return_value=store),
        patch.object(repair._migration, "_migration_lock", lambda _r: nullcontext()),
        patch.object(repair, "_metabase_healthy", return_value=True),
        patch.object(repair, "_redeem_token", return_value=True),
        patch.object(repair, "_finish", return_value={"status": "repaired"}),
        patch.object(repair._migration, "_create_session", return_value="session"),
        patch("dango.visualization.metabase.wait_for_metabase_ready", return_value=True),
        patch("dango.auth.metabase_sync.generate_metabase_password", return_value="pw"),
        patch("dango.platform.docker.get_compose_project_name", return_value="dango-test"),
    ):
        meta.return_value = {"metabase_url": "http://localhost:3000", "admin": {"email": EMAIL}}
        yield


class TestAdminRepair:
    def test_verbs_without_local_compose_file(
        self, shim: Path, project: Path, repair_env: None, timeouts: list[tuple[str, float]]
    ) -> None:
        (shim / "out.ps").write_text(RUNNING)
        (shim / "out.reset-password").write_text(f"noise\nOK [[[{TOKEN}]]]\n")
        assert not (project / "docker-compose.yml").exists()

        result = repair.repair_admin_credential(project)

        assert result == {"status": "repaired"}
        assert verbs(shim) == ["ps", "stop metabase", f"reset-password {EMAIL}", "up"]
        assert docker_calls(shim) == []
        assert_timeouts_cover_budget(timeouts)

    def test_busy_stop_skips_the_cli_and_still_brings_metabase_up(
        self, shim: Path, project: Path, repair_env: None
    ) -> None:
        (shim / "out.ps").write_text(RUNNING)
        (shim / "rc.stop").write_text("75")

        result = repair.repair_admin_credential(project)

        assert result == {"status": "skipped", "reason": "compose_busy"}
        assert verbs(shim) == ["ps", "stop metabase", "up"]
        assert "Docker" in repair.describe_repair_outcome(result)

    def test_not_running_is_skipped(self, shim: Path, project: Path, repair_env: None) -> None:
        (shim / "out.ps").write_text("")

        result = repair.repair_admin_credential(project)

        assert result == {"status": "skipped", "reason": "metabase_not_running"}
        assert verbs(shim) == ["ps"]

    def test_unsupported_email_is_skipped_before_anything_runs(
        self, shim: Path, project: Path, repair_env: None
    ) -> None:
        with patch.object(
            repair,
            "load_metabase_metadata",
            return_value={
                "metabase_url": "http://localhost:3000",
                "admin": {"email": "o'brien@x.io"},
            },
        ):
            result = repair.repair_admin_credential(project)

        assert result == {"status": "skipped", "reason": "unsupported_admin_email"}
        assert verbs(shim) == []
        assert "helper" in repair.describe_repair_outcome(result)

    def test_failed_stop_is_a_cli_failure_not_busy(
        self, shim: Path, project: Path, repair_env: None
    ) -> None:
        (shim / "out.ps").write_text(RUNNING)
        (shim / "rc.stop").write_text("1")

        result = repair.repair_admin_credential(project)

        assert result == {"status": "failed", "reason": "reset_cli_failed"}
        assert verbs(shim) == ["ps", "stop metabase", "up"]

    def test_stop_timeout_is_a_cli_failure_not_busy(
        self, shim: Path, project: Path, repair_env: None
    ) -> None:
        import subprocess

        (shim / "out.ps").write_text(RUNNING)
        real = ce.run_compose

        def run(root: Path, verb: str, *a: str, **kw: Any) -> Any:
            if verb == "stop":
                raise subprocess.TimeoutExpired(verb, 1)
            return real(root, verb, *a, **kw)

        with patch.object(ce, "run_compose", run):
            result = repair.repair_admin_credential(project)

        assert result == {"status": "failed", "reason": "reset_cli_failed"}
        assert verbs(shim) == ["ps", "up"]
