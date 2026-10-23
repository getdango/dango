"""tests/unit/test_scheduled_backup_quoting.py

Quoting contract for the root-run scheduled-backup commands.
The compose project name (read from the server's project.yml) and the Docker
volume path must each reach the program as exactly one value.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import scheduled_backup as sb
from tests.factories.backup_shell import run_spaces_restore

_MODULE = "dango.platform.cloud.scheduled_backup"
_HOSTILE = [
    "dango-x; id",
    "dango-$(id)",
    "dango-`id`",
    "dango-x\nid",
    "dango-x' ; id ; '",
]


def _compose_calls(name: str) -> list[Any]:
    with (
        patch(f"{_MODULE}.get_compose_project_name", return_value=name),
        patch("subprocess.run") as mock_run,
    ):
        sb._stop_services()
        sb._start_services()
    return [c for c in mock_run.call_args_list if c.args[0][0] == "docker"]


@pytest.mark.unit
class TestScheduledBackupComposeQuoting:
    """The compose project name (from the server's project.yml) is one environment value."""

    def test_scheduled_backup_passes_compose_project_in_env(self) -> None:
        calls = _compose_calls("dango-ab12cd34")
        assert len(calls) == 2
        for call in calls:
            assert call.args[0][:4] == [
                "docker",
                "compose",
                "-f",
                f"{sb.PROJECT_DIR}/docker-compose.yml",
            ]
            assert call.kwargs["env"]["COMPOSE_PROJECT_NAME"] == "dango-ab12cd34"
            assert not call.kwargs.get("shell")

        with (
            patch(f"{_MODULE}.get_compose_project_name", return_value="dango-ab12cd34"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="/v\n")) as run,
        ):
            assert sb._get_metabase_volume_path() == "/v"
        assert run.call_args.args[0][:4] == [
            "docker",
            "volume",
            "inspect",
            "dango-ab12cd34_metabase-data",
        ]

    @pytest.mark.parametrize("name", _HOSTILE)
    def test_scheduled_backup_hostile_project_name_is_one_value(self, name: str) -> None:
        for call in _compose_calls(name):
            assert call.kwargs["env"]["COMPOSE_PROJECT_NAME"] == name
            # Nothing from the hostile value became another argument.
            assert call.args[0][:3] == ["docker", "compose", "-f"]
            assert not any(name in arg for arg in call.args[0])

        with (
            patch(f"{_MODULE}.get_compose_project_name", return_value=name),
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")) as run,
        ):
            sb._get_metabase_volume_path()
        args = run.call_args.args[0]
        assert args[:3] == ["docker", "volume", "inspect"]
        assert args[3] == f"{name}_metabase-data"
        assert args[4] == "--format"


@pytest.mark.unit
class TestScheduledBackupVolumePathQuoting:
    """Volume path quoting in the cp commands (backup and restore)."""

    def test_scheduled_backup_volume_path_quoting(self, tmp_path: Path) -> None:
        vol = tmp_path / "it's a vol"
        vol.mkdir()
        (vol / "metabase.db.mv.db").write_text("h2")
        content = tmp_path / "backup-staging"
        (content / "metabase").mkdir(parents=True)
        (content / "metabase" / "metabase.db.mv.db").write_text("h2-archive")
        (content / "manifest.json").write_text("{}")

        _shim, events = run_spaces_restore(
            tmp_path, tmp_path / "project", content.name, metabase_vol=vol
        )

        cps = [e[len("run: ") :] for e in events if e.startswith("run: cp ") and "mv.db" in e]
        assert len(cps) == 1
        tokens = shlex.split(cps[0])
        assert tokens[0] == "cp"
        assert tokens[1].endswith("/metabase/metabase.db.mv.db")
        assert tokens[2] == str(vol) + "/"
        assert (vol / "metabase.db.mv.db").read_text() == "h2-archive"

    def test_spaces_restore_clears_metabase_lock_markers(self, tmp_path: Path) -> None:
        vol = tmp_path / "vol"
        vol.mkdir()
        project = tmp_path / "project"
        state = project / ".dango" / "state"
        state.mkdir(parents=True)
        (state / "metabase_duckdb_lock_applied").write_text("applied\n")
        (state / "metabase_duckdb_lock_attempts").write_text("1\n")
        (state / "other_marker").write_text("keep\n")
        content = tmp_path / "backup-staging"
        (content / "metabase").mkdir(parents=True)
        (content / "metabase" / "metabase.db.mv.db").write_text("h2-archive")
        (content / "manifest.json").write_text("{}")

        run_spaces_restore(tmp_path, project, content.name, metabase_vol=vol)

        assert not (state / "metabase_duckdb_lock_applied").exists()
        assert not (state / "metabase_duckdb_lock_attempts").exists()
        assert (state / "other_marker").read_text() == "keep\n"

    def test_backup_side_copy_survives_quote_in_volume_path(self, tmp_path: Path) -> None:
        vol = tmp_path / "it's a vol"
        vol.mkdir()
        (vol / "metabase.db.mv.db").write_text("h2")
        project = tmp_path / "project"
        project.mkdir()
        recorded: list[str] = []
        copies: list[tuple[Path, Path]] = []

        def fake_run_local(command: str, **_kw: Any) -> str:
            recorded.append(command)
            return ""

        def fake_copy(src: Path, dest: Path, *, step: str) -> None:
            copies.append((src, dest))

        with (
            patch(f"{_MODULE}.PROJECT_DIR", project),
            patch(f"{_MODULE}.BACKUP_DIR", tmp_path / "backups"),
            patch(f"{_MODULE}._checkpoint_databases", return_value=[]),
            patch(f"{_MODULE}._get_metabase_volume_path", return_value=str(vol)),
            patch(f"{_MODULE}._run_local", side_effect=fake_run_local),
            patch(f"{_MODULE}._copy_keeping_modes", side_effect=fake_copy),
            patch(f"{_MODULE}._write_archive"),
            patch("subprocess.run"),
        ):
            sb._create_local_archive("t", include_secrets=False)

        # The H2 copy no longer goes through a shell, so the quote cannot break it.
        assert not any("metabase.db.mv.db" in c for c in recorded)
        h2 = [(src, dest) for src, dest in copies if src.name == "metabase.db.mv.db"]
        assert h2 == [(vol / "metabase.db.mv.db", h2[0][1])]
        assert h2[0][1].parent.name == "metabase"


_EXEC_HOSTILE = [
    "x; touch PWNED1",
    "$(touch PWNED2)",
    "`touch PWNED3`",
    "x\ntouch PWNED4",
    "x' ; touch PWNED5 ; '",
    "a b",
]

_STUB_DOCKER = """#!/usr/bin/env python3
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as fh:
    fh.write(json.dumps({"argv": sys.argv[1:], "env": os.environ.get("COMPOSE_PROJECT_NAME")}) + "\\n")
"""


@pytest.mark.unit
class TestScheduledBackupCommandsExecuted:
    """Run the generated commands (no shell) with a stand-in docker."""

    @pytest.mark.parametrize("name", _EXEC_HOSTILE)
    def test_hostile_name_arrives_as_one_value_and_runs_nothing(
        self, name: str, tmp_path: Path
    ) -> None:
        import json
        import os
        import subprocess as real_subprocess

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "docker"
        stub.write_text(_STUB_DOCKER)
        stub.chmod(0o755)
        log = tmp_path / "docker.log"
        work = tmp_path / "work"
        work.mkdir()

        with (
            patch(f"{_MODULE}.get_compose_project_name", return_value=name),
            patch("subprocess.run") as mock_run,
        ):
            sb._stop_services()
            sb._start_services()
            sb._get_metabase_volume_path()
        calls = [c for c in mock_run.call_args_list if c.args[0][0] == "docker"]
        assert len(calls) == 3

        base_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_LOG": str(log)}
        for call in calls:
            # The call's own environment (compose project name) with the stub docker first.
            env = {**(call.kwargs.get("env") or {}), "PATH": base_env["PATH"], "STUB_LOG": str(log)}
            real_subprocess.run(call.args[0], cwd=work, env=env, check=True)

        records = [json.loads(line) for line in log.read_text().splitlines()]
        assert len(records) == 3
        compose = [r for r in records if r["argv"][0] == "compose"]
        assert len(compose) == 2
        assert all(r["env"] == name for r in compose)
        inspect = [r for r in records if r["argv"][0] == "volume"]
        assert inspect[0]["argv"][:2] == ["volume", "inspect"]
        assert inspect[0]["argv"][2] == f"{name}_metabase-data"
        assert list(work.iterdir()) == []  # no PWNED marker created
        assert not any(p.name.startswith("PWNED") for p in tmp_path.rglob("*"))


@pytest.mark.unit
class TestScheduledBackupRestoreQuoting:
    """Restore commands quote values derived from the Spaces key basename."""

    def test_restore_commands_survive_single_quote_in_key(self, tmp_path: Path) -> None:
        from dango.platform.cloud.backup import BACKUP_FILES

        project = tmp_path / "project"
        project.mkdir()
        content = tmp_path / "it's"
        first = BACKUP_FILES[0]
        (content / first).parent.mkdir(parents=True, exist_ok=True)
        (content / first).write_text("x")

        _shim, events = run_spaces_restore(tmp_path, project, content.name)

        runs = [shlex.split(e[len("run: ") :]) for e in events if e.startswith("run: ")]
        extract = next(t for t in runs if "tar" in t)
        assert extract[:3] == ["tar", "-xzf", str(tmp_path / "backups" / "it's.tar.gz")]
        assert extract[3] == "-C"
        assert re.fullmatch(r".*/dango-restore-staging-[^/]+", extract[4])
        assert "--no-same-permissions" in extract
        # Project files are copied as the service user; every path stays one token.
        copies = [t for t in runs if "cp" in t and str(project / first) + ";" in t]
        assert copies
        cp_at = copies[0].index("cp")
        assert copies[0][cp_at - 4 : cp_at] == ["runuser", "-u", "dango", "--"]
        assert (project / first).read_text() == "x"
