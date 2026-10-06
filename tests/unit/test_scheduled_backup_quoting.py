"""tests/unit/test_scheduled_backup_quoting.py

Shell-quoting contract for the root-run scheduled-backup commands.
The compose project name (read from the server's project.yml) and the Docker
volume path must each reach the shell as exactly one argument.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform.cloud import scheduled_backup as sb

_MODULE = "dango.platform.cloud.scheduled_backup"
_HOSTILE = [
    "dango-x; id",
    "dango-$(id)",
    "dango-`id`",
    "dango-x\nid",
    "dango-x' ; id ; '",
]


def _commands(mock_run: MagicMock) -> list[str]:
    return [call.args[0] for call in mock_run.call_args_list]


def _compose_commands(name: str) -> list[str]:
    with (
        patch(f"{_MODULE}.get_compose_project_name", return_value=name),
        patch("subprocess.run") as mock_run,
    ):
        sb._stop_services()
        sb._start_services()
    return [c for c in _commands(mock_run) if "docker compose" in c]


@pytest.mark.unit
class TestScheduledBackupComposeQuoting:
    """Compose project quoting in stop/start/volume-inspect commands."""

    def test_scheduled_backup_quotes_compose_project(self) -> None:
        compose = _compose_commands("dango-ab12cd34")
        assert len(compose) == 2
        for command in compose:
            assert shlex.split(command)[0] == "COMPOSE_PROJECT_NAME=dango-ab12cd34"
            assert command.startswith("COMPOSE_PROJECT_NAME=dango-ab12cd34 docker compose")

        with (
            patch(f"{_MODULE}.get_compose_project_name", return_value="dango-ab12cd34"),
            patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="/v\n")) as run,
        ):
            assert sb._get_metabase_volume_path() == "/v"
        tokens = shlex.split(run.call_args.args[0])
        assert tokens[:3] == ["docker", "volume", "inspect"]
        assert tokens[3] == "dango-ab12cd34_metabase-data"

    @pytest.mark.parametrize("name", _HOSTILE)
    def test_scheduled_backup_hostile_project_name_is_one_token(self, name: str) -> None:
        for command in _compose_commands(name):
            # `$(..)` and backticks are one token to shlex.split but still expand
            # in a shell unless single-quoted, so require the quoted form.
            assert f"COMPOSE_PROJECT_NAME={shlex.quote(name)} docker" in command
            tokens = shlex.split(command)
            assert tokens[0] == f"COMPOSE_PROJECT_NAME={name}"
            assert tokens[1:4] == ["docker", "compose", "-f"]
            # Nothing from the hostile value escaped into another argument.
            assert not any("id" == t or t.endswith(";") for t in tokens[1:])

        with (
            patch(f"{_MODULE}.get_compose_project_name", return_value=name),
            patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")) as run,
        ):
            sb._get_metabase_volume_path()
        assert shlex.quote(f"{name}_metabase-data") in run.call_args.args[0]
        tokens = shlex.split(run.call_args.args[0])
        assert tokens[:3] == ["docker", "volume", "inspect"]
        assert tokens[3] == f"{name}_metabase-data"
        assert tokens[4] == "--format"


@pytest.mark.unit
class TestScheduledBackupVolumePathQuoting:
    """Volume path quoting in the cp commands (backup and restore)."""

    def test_scheduled_backup_volume_path_quoting(self, tmp_path: Path) -> None:
        vol = tmp_path / "it's a vol"
        vol.mkdir()
        (vol / "metabase.db.mv.db").write_text("h2")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "metabase").mkdir()
        (staging / "metabase" / "metabase.db.mv.db").write_text("h2")
        recorded: list[str] = []

        def fake_run_local(command: str, **_kw: Any) -> str:
            recorded.append(command)
            return ""

        real_path = Path

        def fake_path(value: Any = ".") -> Path:
            text = str(value)
            if text.startswith("/tmp/"):
                return tmp_path / text[len("/tmp/") :]
            return real_path(value)

        with (
            patch(f"{_MODULE}.BACKUP_DIR", tmp_path / "backups"),
            patch(f"{_MODULE}.download_from_spaces"),
            patch(f"{_MODULE}.Path", fake_path),
            patch(f"{_MODULE}._stop_services"),
            patch(f"{_MODULE}._start_services"),
            patch(f"{_MODULE}._run_local", side_effect=fake_run_local),
            patch(f"{_MODULE}._get_metabase_volume_path", return_value=str(vol)),
            patch("subprocess.run"),
        ):
            sb.restore_from_spaces({}, "backups/staging.tar.gz")

        cps = [c for c in recorded if "metabase.db.mv.db" in c]
        assert len(cps) == 1
        tokens = shlex.split(cps[0])
        assert tokens == [
            "cp",
            str(staging / "metabase" / "metabase.db.mv.db"),
            str(vol) + "/",
        ]

    def test_backup_side_copy_survives_quote_in_volume_path(self, tmp_path: Path) -> None:
        vol = tmp_path / "it's a vol"
        vol.mkdir()
        (vol / "metabase.db.mv.db").write_text("h2")
        project = tmp_path / "project"
        project.mkdir()
        recorded: list[str] = []

        def fake_run_local(command: str, **_kw: Any) -> str:
            recorded.append(command)
            return ""

        with (
            patch(f"{_MODULE}.PROJECT_DIR", project),
            patch(f"{_MODULE}.BACKUP_DIR", tmp_path / "backups"),
            patch(f"{_MODULE}._checkpoint_databases", return_value=[]),
            patch(f"{_MODULE}._get_metabase_volume_path", return_value=str(vol)),
            patch(f"{_MODULE}._run_local", side_effect=fake_run_local),
            patch("subprocess.run"),
        ):
            sb._create_local_archive("t", include_secrets=False)

        cps = [c for c in recorded if "metabase.db.mv.db" in c]
        assert len(cps) == 1
        tokens = shlex.split(cps[0])
        assert tokens[0] == "cp"
        assert tokens[1] == str(vol / "metabase.db.mv.db")
        assert tokens[2].endswith("/metabase/")
