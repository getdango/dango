"""tests/unit/test_compose_exec.py

compose_exec: the dango-side wrapper that runs compose through the root-owned helper
when the marker file is present, and keeps the legacy direct argv otherwise.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from dango.config.models import DangoConfig, ProjectContext
from dango.platform import compose_exec as ce
from dango.platform.cloud import compose_helper as ch

pytestmark = pytest.mark.unit

FAKE_SUDO = """#!/bin/sh
d=$(dirname "$0")
printf '%s\\n' "$*" >> "$d/calls"
cat > "$d/stdin"
[ ! -f "$d/err" ] || cat "$d/err" >&2
[ ! -f "$d/out" ] || cat "$d/out"
[ ! -f "$d/hook" ] || sh "$d/hook"
exit "$(cat "$d/rc" 2>/dev/null || echo 0)"
"""


@pytest.fixture
def shim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``sudo`` first on PATH, with a marker the wrapper accepts."""
    d = tmp_path / "shim"
    d.mkdir()
    (d / "sudo").write_text(FAKE_SUDO)
    (d / "sudo").chmod(0o755)
    marker = tmp_path / "active"
    marker.write_text("")
    marker.chmod(0o644)
    monkeypatch.setattr(ce, "MARKER", str(marker))
    monkeypatch.setattr(ce, "_ROOT_UID", os.getuid())
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


def helper_calls(shim: Path) -> list[str]:
    path = shim / "calls"
    return path.read_text().splitlines() if path.exists() else []


class TestConstants:
    def test_match_the_helper_module(self) -> None:
        assert ce.HELPER == ch.HELPER_PATH
        assert ce.MARKER == ch.MARKER


class TestUsesHelper:
    def test_absent_marker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        assert ce.uses_helper() is False

    def test_root_owned_regular_file(self, shim: Path) -> None:
        assert ce.uses_helper() is True

    @pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
    def test_group_or_other_writable(self, shim: Path, mode: int) -> None:
        Path(ce.MARKER).chmod(mode)
        assert ce.uses_helper() is False

    def test_wrong_owner(self, shim: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ce, "_ROOT_UID", os.getuid() + 1)
        assert ce.uses_helper() is False

    def test_symlink_and_directory(self, shim: Path, tmp_path: Path) -> None:
        link = tmp_path / "link"
        link.symlink_to(ce.MARKER)
        with patch.object(ce, "MARKER", str(link)):
            assert ce.uses_helper() is False
        directory = tmp_path / "dir"
        directory.mkdir()
        directory.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        with patch.object(ce, "MARKER", str(directory)):
            assert ce.uses_helper() is False

    def test_ignores_cloud_mode_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        assert ce.uses_helper() is False


class TestLocalPath:
    """Marker absent: exactly today's argv, env, cwd and stdin."""

    @pytest.mark.parametrize(
        ("verb", "args", "tail"),
        [
            ("up", (), ["up", "-d"]),
            ("down", (), ["down"]),
            ("ps", (), ["ps", "--format", "json"]),
            ("stop", ("metabase",), ["stop", "metabase"]),
            ("start", ("metabase",), ["start", "metabase"]),
            ("restart", ("metabase",), ["restart", "metabase"]),
        ],
    )
    def test_argv_per_verb(
        self, tmp_path: Path, verb: str, args: tuple[str, ...], tail: list[str]
    ) -> None:
        env = {"COMPOSE_PROJECT_NAME": "dango-abc"}
        with patch("subprocess.run") as run:
            ce.run_compose(
                tmp_path,
                verb,
                *args,
                timeout=77,
                cwd=tmp_path,
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
            )
        run.assert_called_once_with(
            ["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), *tail],
            cwd=tmp_path,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=77,
            env=env,
        )

    def test_omitted_options_are_not_passed(self, tmp_path: Path) -> None:
        with (
            patch("subprocess.run") as run,
            patch("dango.platform.docker.get_compose_project_name", return_value="dango-1"),
        ):
            ce.run_compose(tmp_path, "stop", "metabase", timeout=90)
        kwargs = run.call_args.kwargs
        assert set(kwargs) == {"capture_output", "timeout", "env"}
        assert kwargs["env"]["COMPOSE_PROJECT_NAME"] == "dango-1"

    def test_compose_v1_prefix_is_kept(self, tmp_path: Path) -> None:
        with patch("subprocess.run") as run:
            ce.run_compose(tmp_path, "down", timeout=5, compose_cmd=["docker-compose"], env={})
        assert run.call_args.args[0][:3] == [
            "docker-compose",
            "-f",
            str(tmp_path / "docker-compose.yml"),
        ]

    def test_image_exists_has_no_local_form(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            ce.run_compose(tmp_path, "image-exists", timeout=5)


class TestHelperPath:
    def test_argv_stdin_and_result(self, shim: Path, tmp_path: Path) -> None:
        (shim / "out").write_text("hello")
        result = ce.run_compose(tmp_path, "stop", "metabase", timeout=1, text=True)
        assert helper_calls(shim) == [f"-n {ch.HELPER_PATH} stop metabase"]
        assert (shim / "stdin").read_text() == ""
        assert result.returncode == 0
        assert result.stdout == "hello"

    def test_bytes_when_not_text(self, shim: Path, tmp_path: Path) -> None:
        (shim / "err").write_text("e")
        result = ce.run_compose(tmp_path, "start", "metabase", timeout=1)
        assert result.stderr == b"e"

    def test_nonzero_exit_is_returned(self, shim: Path, tmp_path: Path) -> None:
        (shim / "rc").write_text("64")
        assert ce.run_compose(tmp_path, "up", timeout=1).returncode == 64

    def test_busy_raises(self, shim: Path, tmp_path: Path) -> None:
        (shim / "rc").write_text("75")
        (shim / "err").write_text("busy")
        with pytest.raises(ce.ComposeBusy):
            ce.run_compose(tmp_path, "start", "metabase", timeout=1)

    @pytest.mark.parametrize("rc", [124, 137, 143])
    def test_killed_or_timed_out_raises_timeout(self, shim: Path, tmp_path: Path, rc: int) -> None:
        (shim / "rc").write_text(str(rc))
        (shim / "err").write_text("dango-compose: up exceeded its time budget (420s)")
        with pytest.raises(subprocess.TimeoutExpired) as info:
            ce.run_compose(tmp_path, "up", timeout=1, text=True)
        assert "time budget" in str(info.value.stderr)

    def test_signalled_wrapper_reports_interrupted_not_budget(
        self, shim: Path, tmp_path: Path
    ) -> None:
        # The helper reports 137 and a budget message even when it was cancelled.
        (shim / "rc").write_text("137")
        (shim / "err").write_text(
            "dango-compose: stop exceeded its time budget (45s)\nreal error\n"
        )
        (shim / "hook").write_text(f"kill -TERM {os.getpid()}\nsleep 0.3\n")
        seen: list[int] = []
        previous = signal.signal(signal.SIGTERM, lambda signum, _frame: seen.append(signum))
        try:
            with pytest.raises(ce.ComposeInterrupted) as info:
                ce.run_compose(tmp_path, "stop", "metabase", timeout=30, text=True)
        finally:
            signal.signal(signal.SIGTERM, previous)
        assert seen == [signal.SIGTERM]
        message = str(info.value)
        assert "interrupted" in message
        assert "time budget" not in message
        assert "real error" in message

    def test_handlers_are_restored(self, shim: Path, tmp_path: Path) -> None:
        before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        ce.run_compose(tmp_path, "up", timeout=1)
        assert {s: signal.getsignal(s) for s in before} == before

    def test_works_off_the_main_thread(self, shim: Path, tmp_path: Path) -> None:
        out: list[int] = []
        t = threading.Thread(
            target=lambda: out.append(ce.run_compose(tmp_path, "up", timeout=5).returncode)
        )
        t.start()
        t.join()
        assert out == [0]

    def test_default_sigterm_ends_the_process_after_the_child(
        self, shim: Path, tmp_path: Path
    ) -> None:
        (shim / "hook").write_text(f"kill -TERM $(cat {shim}/pid)\nsleep 0.3\n")
        code = (
            "import os\nfrom pathlib import Path\nfrom dango.platform import compose_exec as ce\n"
            f"Path({str(shim / 'pid')!r}).write_text(str(os.getpid()))\n"
            f"ce.MARKER = {ce.MARKER!r}\nce._ROOT_UID = {os.getuid()}\n"
            "ce.run_compose(Path('.'), 'up', timeout=30)\nprint('survived')\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
        )
        assert done.returncode == -signal.SIGTERM
        assert "survived" not in done.stdout


class TestTimeouts:
    @pytest.mark.parametrize("verb", sorted(ch.VERB_BUDGET))
    def test_helper_caller_timeout_exceeds_lock_wait_plus_budget(self, verb: str) -> None:
        wait = ch.SUDO_LOCK_WAIT if verb in ch.LOCKED_VERBS else 0
        assert ce.effective_timeout(verb, 1, helper=True) > wait + ch.VERB_BUDGET[verb] + 10

    def test_larger_caller_timeout_is_kept(self) -> None:
        assert ce.effective_timeout("up", 600, helper=True) == 600

    def test_local_timeout_is_untouched(self) -> None:
        assert ce.effective_timeout("up", 1, helper=False) == 1


def make_config(name: str = "My Project") -> DangoConfig:
    config = DangoConfig(project=ProjectContext(name=name, purpose="t", created_by="t"))
    config.platform.metabase_port = 3123
    config.platform.dbt_docs_port = 8123
    return config


class TestRender:
    def test_args_from_config_and_package_default(self, tmp_path: Path) -> None:
        config = make_config()
        args = ce.render_args(tmp_path, config)
        default = (Path(ch.__file__).parents[2] / "templates" / "Dockerfile.metabase").read_text()
        version = next(ln for ln in default.splitlines() if ln.startswith("ENV MB_VERSION=")).split(
            "="
        )[1]
        assert args == ["3123", "8123", version, "my-project"]
        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "dbt" / "target").is_dir()

    def test_version_from_project_dockerfile(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile.metabase").write_text("FROM x\nENV MB_VERSION=v0.99.1\nRUN y\n")
        assert ce.render_args(tmp_path, make_config())[2] == "v0.99.1"

    def test_unparsable_project_version_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile.metabase").write_text("ENV MB_VERSION=$(evil)\n")
        with pytest.raises(ce.ComposeRenderError, match="Dockerfile.metabase"):
            ce.render_args(tmp_path, make_config())

    def test_helper_render_call(self, shim: Path, tmp_path: Path) -> None:
        ce.render(tmp_path, make_config())
        [call] = helper_calls(shim)
        assert call.startswith(f"-n {ch.HELPER_PATH} render 3123 8123 v")
        assert call.endswith(" my-project")

    def test_force_helper_without_marker(self, shim: Path, tmp_path: Path) -> None:
        Path(ce.MARKER).unlink()
        ce.render(tmp_path, make_config(), force_helper=True)
        assert len(helper_calls(shim)) == 1

    def test_render_failure_raises_with_helper_message(self, shim: Path, tmp_path: Path) -> None:
        (shim / "rc").write_text("66")
        (shim / "err").write_text("dango-compose: render: bad name")
        with pytest.raises(ce.ComposeRenderError, match="bad name"):
            ce.render(tmp_path, make_config())

    def test_local_render_is_the_legacy_function(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ce, "MARKER", str(tmp_path / "nope"))
        config = make_config()
        with patch("dango.platform.docker.render_docker_compose") as legacy:
            ce.render(tmp_path, config)
        legacy.assert_called_once_with(tmp_path, config)

    def test_pinned_id_mismatch_warns_and_still_renders(
        self, shim: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pinned = tmp_path / "project-id"
        pinned.write_text("deadbeef-0000\n")
        with patch.object(ce, "PROJECT_ID_FILE", str(pinned)):
            with caplog.at_level("WARNING"):
                ce.render(tmp_path, make_config())
        assert "pin-project" in caplog.text
        assert len(helper_calls(shim)) == 1

    def test_matching_pinned_id_is_silent(
        self, shim: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config = make_config()
        pinned = tmp_path / "project-id"
        pinned.write_text(f"{config.project.id}\n")
        with patch.object(ce, "PROJECT_ID_FILE", str(pinned)), caplog.at_level("WARNING"):
            ce.render(tmp_path, config)
        assert "pin-project" not in caplog.text


class TestCli:
    def test_render_helper_flag(self, tmp_path: Path) -> None:
        with (
            patch("dango.config.helpers.load_config", return_value=make_config()),
            patch.object(ce, "render") as render,
        ):
            assert ce.main(["render", "--helper", str(tmp_path)]) == 0
        assert render.call_args.kwargs == {"force_helper": True}

    def test_render_without_flag(self, tmp_path: Path) -> None:
        with (
            patch("dango.config.helpers.load_config", return_value=make_config()),
            patch.object(ce, "render") as render,
        ):
            assert ce.main(["render", str(tmp_path)]) == 0
        assert render.call_args.kwargs == {"force_helper": False}

    def test_failure_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("dango.config.helpers.load_config", return_value=make_config()),
            patch.object(ce, "render", side_effect=ce.ComposeRenderError("nope")),
        ):
            assert ce.main(["render", "--helper", str(tmp_path)]) == 1
        assert "nope" in capsys.readouterr().err

    def test_usage(self) -> None:
        assert ce.main([]) == 2
        assert ce.main(["bogus", "x"]) == 2

    def test_runs_under_isolated_python(self, tmp_path: Path) -> None:
        done = subprocess.run(
            [sys.executable, "-I", "-m", "dango.platform.compose_exec"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            cwd=tmp_path,
        )
        assert done.returncode == 2
        assert "usage" in done.stderr.lower()
