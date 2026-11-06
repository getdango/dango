"""tests/unit/test_run_as_command.py

run_as: the dango-side builder for running user code as a restricted OS user.
Cloud mode builds ``sudo -n -u <runner> ... isolated_run``; local mode keeps the
legacy command and environment.  Nothing here touches a real sudo.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dango.platform import run_as

pytestmark = pytest.mark.unit

SECRET = "s3cr3t-value-XYZ"
OTHER = "other-value-ABC"


@pytest.fixture
def marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "active"
    path.write_text("")
    path.chmod(0o644)
    monkeypatch.setattr(run_as, "MARKER", str(path))
    monkeypatch.setattr(run_as, "_ROOT_UID", os.getuid())
    monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".env").write_text(f"A_TOKEN={SECRET}\nB_TOKEN={OTHER}\nC_TOKEN='quoted value'\n")
    return root


class TestConstants:
    def test_roles(self) -> None:
        assert run_as.runner_user("script") == "dango-run"
        assert run_as.runner_user("nb") == "dango-nb"
        with pytest.raises(ValueError):
            run_as.runner_user("root")

    def test_paths(self) -> None:
        assert run_as.PYTHON == "/srv/dango/venv/bin/python"
        assert run_as.MARKER == "/srv/dango/runner/active"


class TestUsesIsolation:
    def test_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_as, "MARKER", str(tmp_path / "nope"))
        assert run_as.uses_isolation() is False

    def test_root_owned_regular_file(self, marker: Path) -> None:
        assert run_as.uses_isolation() is True

    @pytest.mark.parametrize("mode", [0o664, 0o646, 0o666])
    def test_world_or_group_writable_refused(self, marker: Path, mode: int) -> None:
        marker.chmod(mode)
        assert run_as.uses_isolation() is False

    def test_symlink_refused(
        self, marker: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = tmp_path / "link"
        link.symlink_to(marker)
        monkeypatch.setattr(run_as, "MARKER", str(link))
        assert run_as.uses_isolation() is False

    def test_directory_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_as, "MARKER", str(tmp_path))
        assert run_as.uses_isolation() is False

    def test_wrong_owner_refused(self, marker: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(run_as, "_ROOT_UID", os.getuid() + 1)
        assert run_as.uses_isolation() is False

    def test_ignores_cloud_mode_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DANGO_CLOUD_MODE", "true")
        monkeypatch.setattr(run_as, "MARKER", str(tmp_path / "nope"))
        assert run_as.uses_isolation() is False


class TestResolveDeclared:
    def test_reads_only_declared_names_from_dotenv(self, project: Path) -> None:
        values, missing = run_as.resolve_declared(project, ["A_TOKEN", "C_TOKEN"])
        assert values == {"A_TOKEN": SECRET, "C_TOKEN": "quoted value"}
        assert missing == ()

    def test_ignores_process_environment(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ONLY_IN_PROCESS", "x")
        values, missing = run_as.resolve_declared(project, ["ONLY_IN_PROCESS"])
        assert values == {}
        assert missing == ("ONLY_IN_PROCESS",)

    def test_declared_but_unset_is_reported(self, project: Path) -> None:
        values, missing = run_as.resolve_declared(project, ["A_TOKEN", "NOPE"])
        assert "NOPE" not in values
        assert missing == ("NOPE",)

    def test_missing_dotenv(self, tmp_path: Path) -> None:
        assert run_as.resolve_declared(tmp_path, ["A_TOKEN"]) == ({}, ("A_TOKEN",))

    def test_dotenv_symlink_is_not_followed(self, project: Path, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere"
        target.write_text(f"A_TOKEN={SECRET}\n")
        (project / ".env").unlink()
        (project / ".env").symlink_to(target)
        assert run_as.resolve_declared(project, ["A_TOKEN"]) == ({}, ("A_TOKEN",))

    def test_explicit_text_is_used(self, tmp_path: Path) -> None:
        values, _ = run_as.resolve_declared(tmp_path, ["A_TOKEN"], env_text="A_TOKEN=zz\n")
        assert values == {"A_TOKEN": "zz"}

    @pytest.mark.parametrize(
        "name", ["PATH", "DANGO_X", "lower", "LD_PRELOAD", "PYTHONPATH", "A_TOKEN\n"]
    )
    def test_reserved_or_invalid_names_rejected(self, project: Path, name: str) -> None:
        with pytest.raises(ValueError):
            run_as.resolve_declared(project, [name])


class TestBuildEnv:
    def test_exactly_base_plus_declared(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak")
        monkeypatch.setenv("DANGO_ADMIN_PASSWORD", "leak")
        env = run_as.build_env("script", project, {"A_TOKEN": SECRET}, run_id="r1")
        assert set(env) == {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "DANGO_PROJECT_ROOT",
            "DANGO_WORKSPACE",
            "DANGO_CLOUD_MODE",
            "DANGO_SCRIPT_RUN_ID",
            "A_TOKEN",
        }
        assert env["A_TOKEN"] == SECRET
        assert env["PATH"] == "/srv/dango/venv/bin:/usr/bin:/bin"
        assert env["HOME"] == "/srv/dango/runner/scripts"
        assert env["DANGO_WORKSPACE"] == "/srv/dango/runner/scripts"
        assert env["DANGO_PROJECT_ROOT"] == str(project)
        assert env["DANGO_CLOUD_MODE"] == "true"
        assert env["DANGO_SCRIPT_RUN_ID"] == "r1"

    def test_nb_role_has_no_declared_values_and_nb_home(self, project: Path) -> None:
        env = run_as.build_env("nb", project, {"A_TOKEN": SECRET})
        assert "A_TOKEN" not in env
        assert env["HOME"] == "/srv/dango/runner/nb-home"
        assert "DANGO_SCRIPT_RUN_ID" not in env

    def test_tz_only_when_given(self, project: Path) -> None:
        assert "TZ" not in run_as.build_env("script", project, {})
        assert run_as.build_env("script", project, {}, tz="UTC")["TZ"] == "UTC"

    def test_declared_cannot_override_base(self, project: Path) -> None:
        with pytest.raises(ValueError):
            run_as.build_env("script", project, {"PATH": "/evil"})

    def test_does_not_copy_process_env(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SOME_UNRELATED", "1")
        assert "SOME_UNRELATED" not in run_as.build_env("script", project, {})


class TestBuildCommand:
    def test_script_command_shape(self, project: Path) -> None:
        env = run_as.build_env("script", project, {"A_TOKEN": SECRET}, run_id="r1")
        cmd = run_as.build_command(
            "script", ["scripts/reports/x.py", "--flag"], env_keys=env, cwd=project
        )
        assert cmd[:6] == ["/usr/bin/sudo", "-n", "-u", "dango-run", "-H", cmd[5]]
        assert cmd[5].startswith("--preserve-env=")
        tail = cmd[6:]
        assert tail == [
            "/srv/dango/venv/bin/python",
            "-I",
            "-m",
            "dango.runtime.isolated_run",
            "--role",
            "script",
            "--cwd",
            str(project),
            "--",
            "scripts/reports/x.py",
            "--flag",
        ]

    def test_preserve_env_equals_env_keys(self, project: Path) -> None:
        env = run_as.build_env("script", project, {"A_TOKEN": SECRET, "B_TOKEN": OTHER})
        cmd = run_as.build_command("script", ["scripts/x.py"], env_keys=env, cwd=project)
        listed = cmd[5].split("=", 1)[1].split(",")
        assert sorted(listed) == sorted(env)

    def test_nb_role_uses_nb_user(self, project: Path) -> None:
        env = run_as.build_env("nb", project, {})
        cmd = run_as.build_command("nb", ["edit", "--port", "7805"], env_keys=env, cwd=project)
        assert cmd[3] == "dango-nb"
        assert cmd[cmd.index("--role") + 1] == "nb"
        assert cmd[-3:] == ["edit", "--port", "7805"]

    def test_never_a_shell(self, project: Path) -> None:
        env = run_as.build_env("script", project, {})
        cmd = run_as.build_command("script", ["scripts/x.py"], env_keys=env, cwd=project)
        assert all(isinstance(part, str) for part in cmd)
        assert "sh" not in cmd and "-c" not in cmd and "bash" not in cmd

    def test_unknown_role(self, project: Path) -> None:
        with pytest.raises(ValueError):
            run_as.build_command("root", ["x"], env_keys={}, cwd=project)

    def test_rejects_option_like_env_key(self, project: Path) -> None:
        with pytest.raises(ValueError):
            run_as.build_command("script", ["x"], env_keys={"A,B": "1"}, cwd=project)
        with pytest.raises(ValueError):
            run_as.build_command("script", ["x"], env_keys={"OK\n": "1"}, cwd=project)


class TestBuildLaunch:
    def test_cloud_argv_never_contains_a_declared_value(self, marker: Path, project: Path) -> None:
        launch = run_as.build_launch(
            "script",
            ["scripts/x.py"],
            project_root=project,
            names=["A_TOKEN", "B_TOKEN", "C_TOKEN"],
            legacy_argv=["python", "scripts/x.py"],
            run_id="r9",
        )
        assert launch.isolated is True
        for value in (SECRET, OTHER, "quoted value"):
            assert not any(value in part for part in launch.argv), value
        assert launch.env["A_TOKEN"] == SECRET
        assert launch.env["B_TOKEN"] == OTHER
        listed = launch.argv[5].split("=", 1)[1].split(",")
        assert sorted(listed) == sorted(launch.env)

    def test_cloud_env_is_empty_plus_declared_only(
        self, marker: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEAKY", "1")
        launch = run_as.build_launch(
            "script",
            ["scripts/x.py"],
            project_root=project,
            names=["A_TOKEN"],
            legacy_argv=["python", "scripts/x.py"],
        )
        assert "LEAKY" not in launch.env
        assert "B_TOKEN" not in launch.env
        assert launch.missing == ()

    def test_missing_names_are_reported_not_set(self, marker: Path, project: Path) -> None:
        launch = run_as.build_launch(
            "script",
            ["scripts/x.py"],
            project_root=project,
            names=["A_TOKEN", "NOPE"],
            legacy_argv=["python"],
        )
        assert launch.missing == ("NOPE",)
        assert "NOPE" not in launch.env
        assert "NOPE" not in launch.argv[5]

    def test_local_mode_is_legacy(
        self, tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(run_as, "MARKER", str(tmp_path / "nope"))
        monkeypatch.setenv("LEGACY_VAR", "yes")
        launch = run_as.build_launch(
            "script",
            ["scripts/x.py"],
            project_root=project,
            names=["A_TOKEN"],
            legacy_argv=["python", "scripts/x.py"],
        )
        assert launch.isolated is False
        assert launch.argv == ["python", "scripts/x.py"]
        assert launch.env["LEGACY_VAR"] == "yes"
        assert "A_TOKEN" not in launch.env
        assert launch.missing == ()


class _Recorder:
    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        return subprocess.CompletedProcess(cmd, self.returncode, "", "")


class TestStop:
    def test_group_stop_uses_negative_pgid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder()
        monkeypatch.setattr(run_as.subprocess, "run", rec)
        monkeypatch.setattr(run_as, "_group_alive", lambda pgid: False)
        remaining = run_as.stop_group("script", 4242, grace=0.2)
        assert remaining is False
        assert rec.calls == [
            ["/usr/bin/sudo", "-n", "-u", "dango-run", "/usr/bin/kill", "-TERM", "--", "-4242"]
        ]

    def test_group_stop_escalates_to_kill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder()
        monkeypatch.setattr(run_as.subprocess, "run", rec)
        state = {"n": 0}

        def alive(pgid: int) -> bool:
            state["n"] += 1
            return state["n"] < 30

        monkeypatch.setattr(run_as, "_group_alive", alive)
        monkeypatch.setattr(run_as.time, "sleep", lambda s: None)
        run_as.stop_group("script", 7, grace=0.5)
        assert [c[5] for c in rec.calls[:1]] == ["-TERM"]
        assert any(c[5] == "-KILL" and c[-1] == "-7" for c in rec.calls)

    def test_group_rejects_bad_pgid(self) -> None:
        for bad in (0, 1, -5):
            with pytest.raises(ValueError):
                run_as.stop_group("script", bad)

    def test_stop_all_targets_every_runner_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder()
        monkeypatch.setattr(run_as.subprocess, "run", rec)
        monkeypatch.setattr(run_as, "runner_pids", lambda role: [])
        assert run_as.stop_all("nb", grace=0.2) == []
        assert rec.calls == [
            ["/usr/bin/sudo", "-n", "-u", "dango-nb", "/usr/bin/kill", "-TERM", "--", "-1"]
        ]

    def test_stop_all_returns_survivors_after_kill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder()
        monkeypatch.setattr(run_as.subprocess, "run", rec)
        monkeypatch.setattr(run_as, "runner_pids", lambda role: [11, 12])
        monkeypatch.setattr(run_as.time, "sleep", lambda s: None)
        assert run_as.stop_all("script", grace=0.1) == [11, 12]
        verbs = [c[5] for c in rec.calls]
        assert verbs == ["-TERM", "-KILL"]

    def test_stop_all_with_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder()
        monkeypatch.setattr(run_as.subprocess, "run", rec)
        monkeypatch.setattr(run_as, "_group_alive", lambda pgid: False)
        assert run_as.stop_all("script", pgid=99, grace=0.1) == []
        assert rec.calls[0][-1] == "-99"

    def test_sudo_failure_is_not_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = _Recorder(returncode=1)
        monkeypatch.setattr(run_as.subprocess, "run", rec)
        monkeypatch.setattr(run_as, "runner_pids", lambda role: [5])
        monkeypatch.setattr(run_as.time, "sleep", lambda s: None)
        assert run_as.stop_all("nb", grace=0.1) == [5]

    def test_group_alive_probe(self) -> None:
        proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            assert run_as._group_alive(proc.pid) is True
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        assert run_as._group_alive(proc.pid) is False


class TestRunnerUid:
    @pytest.mark.parametrize("uid", [0, 1, 999, 61000, 61001, 999, 500, 999999])
    def test_forbidden(self, uid: int) -> None:
        with pytest.raises(ValueError):
            run_as.assert_runner_uid(uid)

    @pytest.mark.parametrize("uid", [1000, 1001, 5000, 59999])
    def test_allowed(self, uid: int) -> None:
        assert run_as.assert_runner_uid(uid) == uid

    def test_both_runners_must_differ(self) -> None:
        with pytest.raises(ValueError):
            run_as.assert_distinct_uids(1001, 1001)
        run_as.assert_distinct_uids(1001, 1002)

    def test_runner_uid_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import pwd

        class Entry:
            pw_uid = 1002

        monkeypatch.setattr(pwd, "getpwnam", lambda name: Entry())
        assert run_as.runner_uid("nb") == 1002
