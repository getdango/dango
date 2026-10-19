"""tests/unit/test_compose_exec_routing.py

With the marker present, DockerManager, start_docker_services and the Metabase
stop/start helpers reach Docker only through ``sudo -n dango-compose <verb>``; with the
marker absent they keep their direct ``docker compose`` calls.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.platform import compose_exec as ce
from dango.platform.cloud import compose_helper as ch
from dango.platform.common import metabase_lifecycle as ml
from dango.platform.docker import DockerManager, ServiceStatus

pytestmark = pytest.mark.unit

FAKE_TOOL = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/$(basename "$0").calls"
cat >/dev/null
if [ "$(basename "$0")" = sudo ] && [ -f "$d/out.$4" ]; then cat "$d/out.$4"; fi
exit "$(cat "$d/rc.$4" 2>/dev/null || echo 0)"
"""


@pytest.fixture
def helper_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "shim"
    d.mkdir()
    for name in ("sudo", "docker"):
        (d / name).write_text(FAKE_TOOL)
        (d / name).chmod(0o755)
    marker = tmp_path / "active"
    marker.write_text("")
    marker.chmod(0o644)
    monkeypatch.setattr(ce, "MARKER", str(marker))
    monkeypatch.setattr(ce, "_ROOT_UID", os.getuid())
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(ml.time, "sleep", lambda _s: None)
    return d


def sudo_verbs(d: Path) -> list[str]:
    path = d / "sudo.calls"
    if not path.exists():
        return []
    return [
        line.split()[2:] and " ".join(line.split()[2:]) for line in path.read_text().splitlines()
    ]


def docker_calls(d: Path) -> list[str]:
    path = d / "docker.calls"
    return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def lock_verb(d: Path) -> None:
    """Make fake sudo answer the first call (image-exists) with an image id."""
    (d / "out.image-exists").write_text("sha256:abc\n")


class TestDockerManagerWithHelper:
    def test_start_services(self, helper_env: Path, project: Path) -> None:
        lock_verb(helper_env)
        assert DockerManager(project).start_services() is True
        assert sudo_verbs(helper_env) == [f"{ch.HELPER_PATH} image-exists", f"{ch.HELPER_PATH} up"]
        assert docker_calls(helper_env) == []

    def test_start_services_failure_is_reported(self, helper_env: Path, project: Path) -> None:
        lock_verb(helper_env)
        (helper_env / "rc.up").write_text("1")
        assert DockerManager(project).start_services() is False

    def test_start_services_busy_is_a_failure_not_a_crash(
        self, helper_env: Path, project: Path
    ) -> None:
        lock_verb(helper_env)
        (helper_env / "rc.up").write_text("75")
        assert DockerManager(project).start_services() is False

    def test_first_run_when_no_image(self, helper_env: Path, project: Path) -> None:
        with patch("dango.platform.docker.console") as console:
            DockerManager(project).start_services()
        assert any("first run" in str(c) for c in console.print.call_args_list)

    def test_stop_services(self, helper_env: Path, project: Path) -> None:
        assert DockerManager(project).stop_services() is True
        assert sudo_verbs(helper_env) == [f"{ch.HELPER_PATH} down"]
        assert docker_calls(helper_env) == []

    def test_service_status(self, helper_env: Path, project: Path) -> None:
        (helper_env / "out.ps").write_text(
            '{"Service": "metabase", "State": "running", "Health": ""}\n'
            '{"Service": "dbt-docs", "State": "exited"}\n'
        )
        assert DockerManager(project).get_service_status() == {
            "metabase": ServiceStatus.RUNNING,
            "dbt-docs": ServiceStatus.STOPPED,
        }
        assert docker_calls(helper_env) == []

    def test_stop_all_stops_only_the_pinned_project(self, helper_env: Path, project: Path) -> None:
        assert DockerManager(project).stop_all_dango_containers() is True
        assert sudo_verbs(helper_env) == [f"{ch.HELPER_PATH} stop"]
        assert docker_calls(helper_env) == []

    def test_stop_all_projects_is_not_offered(self, helper_env: Path, project: Path) -> None:
        assert DockerManager(project).stop_all_dango_containers(all_projects=True) is False
        assert sudo_verbs(helper_env) == []
        assert docker_calls(helper_env) == []


class TestDockerManagerWithoutHelper:
    def test_start_uses_direct_compose(
        self, tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        (project / "docker-compose.yml").write_text("services: {}\n")
        manager = DockerManager(project)
        run = MagicMock(return_value=MagicMock(returncode=0, stdout="x", stderr=""))
        with (
            patch.object(manager, "_resolve_or_migrate_project_id"),
            patch.object(manager, "_assert_no_identity_collision"),
            patch.object(manager, "is_docker_available", return_value=True),
            patch.object(manager, "is_compose_available", return_value=True),
            patch.object(manager, "get_compose_command", return_value=["docker", "compose"]),
            patch.object(manager, "_metabase_image_exists", return_value=True),
            patch("subprocess.run", run),
            patch("dango.platform.docker.console"),
        ):
            assert manager.start_services() is True
        assert run.call_args.args[0] == [
            "docker",
            "compose",
            "-f",
            str(project / "docker-compose.yml"),
            "up",
            "-d",
        ]
        assert run.call_args.kwargs["cwd"] == project
        assert run.call_args.kwargs["timeout"] == 120


def make_manager() -> MagicMock:
    manager = MagicMock()
    manager.is_docker_daemon_running.return_value = False  # dango cannot run docker
    manager.start_services.return_value = True
    return manager


class TestStartDockerServicesWithHelper:
    def test_no_daemon_check_and_render_goes_through_the_helper(
        self, helper_env: Path, project: Path
    ) -> None:
        from dango.config.models import DangoConfig, ProjectContext
        from dango.platform.common.startup import start_docker_services

        config = DangoConfig(project=ProjectContext(name="P", purpose="t", created_by="t"))
        sock = MagicMock()
        sock.connect_ex.return_value = 1  # ports free
        with (
            patch("dango.config.helpers.load_config", return_value=config),
            patch("dango.platform.common.startup.socket.socket", return_value=sock),
            patch("dango.platform.docker.console"),
        ):
            lock_verb(helper_env)
            start_docker_services(project)
        verbs = [v.split()[1] for v in sudo_verbs(helper_env)]
        assert verbs == ["down", "render", "image-exists", "up"]
        assert docker_calls(helper_env) == []
        assert not (project / "docker-compose.yml").exists()

    def test_render_failure_does_not_stop_the_start(self, helper_env: Path, project: Path) -> None:
        from dango.config.models import DangoConfig, ProjectContext
        from dango.platform.common.startup import start_docker_services

        config = DangoConfig(project=ProjectContext(name="P", purpose="t", created_by="t"))
        (helper_env / "rc.render").write_text("66")
        sock = MagicMock()
        sock.connect_ex.return_value = 1
        lock_verb(helper_env)
        with (
            patch("dango.config.helpers.load_config", return_value=config),
            patch("dango.platform.common.startup.socket.socket", return_value=sock),
            patch("dango.platform.docker.console"),
        ):
            start_docker_services(project)
        assert [v.split()[1] for v in sudo_verbs(helper_env)][-1] == "up"

    def test_local_still_checks_the_daemon(
        self, tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.platform.common.startup import start_docker_services

        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        manager = make_manager()
        with patch("dango.platform.DockerManager", return_value=manager):
            with pytest.raises(RuntimeError, match="Docker daemon"):
                start_docker_services(project)

    def test_local_render_is_the_legacy_function(
        self, tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.platform.common.startup import start_docker_services

        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        manager = make_manager()
        manager.is_docker_daemon_running.return_value = True
        sock = MagicMock()
        sock.connect_ex.return_value = 1
        with (
            patch("dango.platform.DockerManager", return_value=manager),
            patch("dango.platform.common.startup.socket.socket", return_value=sock),
            patch("dango.config.helpers.load_config", return_value=MagicMock()),
            patch("dango.platform.docker.render_docker_compose") as legacy,
        ):
            start_docker_services(project)
        legacy.assert_called_once()


class TestMetabaseLifecycle:
    def test_timeouts_exceed_the_helper_lock_wait_plus_budget(self) -> None:
        assert ml.STOP_TIMEOUT >= 90
        assert ml.START_TIMEOUT >= 150
        assert ml.STOP_TIMEOUT > ch.SUDO_LOCK_WAIT + ch.VERB_BUDGET["stop"] + 10
        assert ml.START_TIMEOUT > ch.SUDO_LOCK_WAIT + ch.VERB_BUDGET["start"] + 10

    def test_stop_and_start_go_through_the_helper(self, helper_env: Path, project: Path) -> None:
        assert ml.stop_metabase_for_writes(project, force=True) is True
        assert ml.start_metabase_after_writes(project) is True
        assert sudo_verbs(helper_env) == [
            f"{ch.HELPER_PATH} stop metabase",
            f"{ch.HELPER_PATH} start metabase",
        ]
        assert docker_calls(helper_env) == []

    def test_busy_stop_is_not_reported_as_stopped(self, helper_env: Path, project: Path) -> None:
        (helper_env / "rc.stop").write_text("75")
        assert ml.stop_metabase_for_writes(project, force=True) is False

    def test_busy_start_is_reported(self, helper_env: Path, project: Path) -> None:
        (helper_env / "rc.start").write_text("75")
        assert ml.start_metabase_after_writes(project) is False

    def test_local_argv_env_and_timeouts(
        self, tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        monkeypatch.setattr(ml.time, "sleep", lambda _s: None)
        with (
            patch("subprocess.run", return_value=MagicMock(returncode=0)) as run,
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-zz"),
        ):
            ml.stop_metabase_for_writes(project, force=True)
            ml.start_metabase_after_writes(project)
        stop, start = run.call_args_list
        for call, verb, timeout in (
            (stop, "stop", ml.STOP_TIMEOUT),
            (start, "start", ml.START_TIMEOUT),
        ):
            assert call.args[0] == [
                "docker",
                "compose",
                "-f",
                str(project / "docker-compose.yml"),
                verb,
                "metabase",
            ]
            assert call.kwargs["timeout"] == timeout
            assert call.kwargs["env"]["COMPOSE_PROJECT_NAME"] == "dango-zz"
            assert call.kwargs["capture_output"] is True

    def test_not_applicable_outside_cloud_mode(
        self, helper_env: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("DANGO_CLOUD_MODE", raising=False)
        assert ml.stop_metabase_for_writes(project) is False
        assert sudo_verbs(helper_env) == []
