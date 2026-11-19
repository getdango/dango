"""tests/unit/test_cleanup_honest_results.py

`dango cleanup` deletes only what it listed, reports each failure, exits non-zero when
something the user asked to remove stayed, and never touches Docker volumes of other projects.
"""

from __future__ import annotations

import gzip
import os
import re
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.cleanup import cleanup
from dango.platform.docker import get_compose_project_name

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _old_archive(log_dir: Path, name: str) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    archive = log_dir / name
    archive.write_bytes(gzip.compress(b"x" * 64))
    old = time.time() - 120 * 86_400
    os.utime(archive, (old, old))
    return archive


def _invoke(project: Path, args: list[str]):
    with patch("dango.cli.utils.require_project_context", return_value=project):
        return CliRunner().invoke(cleanup, args, obj={"project_root": str(project)})


@pytest.mark.unit
def test_log_cleanup_removes_only_the_archives_it_listed(tmp_path: Path) -> None:
    logs = tmp_path / ".dango" / "logs"
    listed = _old_archive(logs, "audit.20200101.jsonl.gz")
    unlisted = _old_archive(logs, "audit.20200102.jsonl.gz")

    with patch(
        "dango.cli.commands.cleanup._collect_log_archives",
        return_value=[(listed, listed.stat().st_size)],
    ):
        result = _invoke(tmp_path, ["--yes", "--logs-only"])

    assert result.exit_code == 0, result.output
    assert not listed.exists()
    assert unlisted.exists(), "an archive that was never listed or confirmed was deleted"


@pytest.mark.unit
def test_failed_log_archive_removal_is_not_reported_as_success(tmp_path: Path) -> None:
    logs = tmp_path / ".dango" / "logs"
    stuck = _old_archive(logs, "audit.20200101.jsonl.gz")
    real_unlink = Path.unlink

    def fake_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == stuck:
            raise PermissionError("in use")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "unlink", fake_unlink):
        result = _invoke(tmp_path, ["--yes", "--logs-only"])

    out = _strip(result.output)
    assert stuck.exists()
    assert "Removed 1 log archive" not in out
    assert "audit.20200101.jsonl.gz" in out
    assert result.exit_code != 0


@pytest.mark.unit
def test_failed_pycache_removal_is_reported_and_exits_non_zero(tmp_path: Path) -> None:
    cache = tmp_path / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "m.pyc").write_bytes(b"x")

    with patch("shutil.rmtree", side_effect=OSError("Permission denied")):
        result = _invoke(tmp_path, ["--yes"])

    out = _strip(result.output)
    assert cache.exists()
    assert "Permission denied" in out
    assert result.exit_code != 0


@pytest.mark.unit
def test_failed_dbt_artifact_removal_exits_non_zero(tmp_path: Path) -> None:
    target = tmp_path / "dbt" / "target"
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}", encoding="utf-8")

    with patch("shutil.rmtree", side_effect=OSError("Permission denied")):
        result = _invoke(tmp_path, ["--yes"])

    assert "Failed to remove" in _strip(result.output)
    assert result.exit_code != 0


def _docker_run(volumes: list[str], removed: list[str]):
    def run(argv: list[str], *args: object, **kwargs: object) -> MagicMock:
        if argv[:3] == ["docker", "volume", "ls"]:
            return MagicMock(returncode=0, stdout="\n".join(volumes) + "\n")
        if argv[:3] == ["docker", "volume", "rm"]:
            removed.append(argv[3])
        return MagicMock(returncode=0, stdout="")

    return run


@pytest.mark.unit
def test_docker_flag_never_removes_another_projects_volumes(tmp_path: Path) -> None:
    mine = f"{get_compose_project_name(tmp_path)}_metabase-data"
    foreign = "customer_app_pgdata"
    removed: list[str] = []

    with patch("subprocess.run", side_effect=_docker_run([foreign, mine], removed)):
        result = _invoke(tmp_path, ["--yes", "--docker"])

    assert result.exit_code == 0, result.output
    assert removed == [mine]


@pytest.mark.unit
def test_docker_volume_names_are_shown_before_confirmation(tmp_path: Path) -> None:
    mine = f"{get_compose_project_name(tmp_path)}_metabase-data"
    removed: list[str] = []

    with (
        patch("subprocess.run", side_effect=_docker_run([mine], removed)),
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
    ):
        result = CliRunner().invoke(
            cleanup, ["--docker"], input="n\n", obj={"project_root": str(tmp_path)}
        )

    assert mine in _strip(result.output)
    assert removed == []
