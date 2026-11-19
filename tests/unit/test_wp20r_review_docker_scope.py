"""tests/unit/test_wp20r_review_docker_scope.py

`cleanup --docker` keeps declared volumes and says so; `docker-audit` never offers the current project for removal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.cleanup import cleanup
from dango.cli.commands.docker_audit import docker_audit
from dango.platform.docker import get_compose_project_name

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    return " ".join(_ANSI_RE.sub("", result.output).split())


def _project(tmp_path: Path, declared: tuple[str, ...] = ("metabase-data",)) -> str:
    lines = ["services: {}", "volumes:"]
    for name in declared:
        lines += [f"  {name}:", "    driver: local"]
    (tmp_path / "docker-compose.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return get_compose_project_name(tmp_path)


def _docker(volumes: list[str], removed: list[str]):
    def run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        if argv[:3] == ["docker", "volume", "ls"]:
            return MagicMock(returncode=0, stdout="\n".join(volumes) + "\n")
        if argv[:3] == ["docker", "volume", "rm"]:
            removed.append(argv[3])
        return MagicMock(returncode=0, stdout="")

    return run


def _cleanup(tmp_path: Path, volumes: list[str], args: list[str], removed: list[str], **kw):
    with (
        patch("subprocess.run", side_effect=_docker(volumes, removed)),
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
    ):
        return CliRunner().invoke(cleanup, args, obj={"project_root": str(tmp_path)}, **kw)


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--docker"], ["--docker", "--yes"]])
def test_a_declared_volume_is_kept_and_named(tmp_path: Path, args: list[str]) -> None:
    prefix = _project(tmp_path)
    declared = f"{prefix}_metabase-data"
    removed: list[str] = []
    result = _cleanup(tmp_path, [declared], args, removed, input="y\n")
    assert removed == []
    out = _out(result)
    assert declared in out and "never removes them" in out
    assert "docker volume rm" not in out  # Dango never advises deleting Metabase data


@pytest.mark.unit
@pytest.mark.parametrize("args", [["--docker", "--yes"], ["--docker"]])
def test_an_undeclared_leftover_is_listed_and_removed_declared_one_kept(
    tmp_path: Path, args: list[str]
) -> None:
    prefix = _project(tmp_path)
    declared = f"{prefix}_metabase-data"
    leftover = f"{prefix}_old-cache"
    removed: list[str] = []
    result = _cleanup(tmp_path, [declared, leftover, "other_pgdata"], args, removed, input="y\n")
    assert removed == [leftover]
    out = _out(result)
    assert leftover in out and declared in out


@pytest.mark.unit
def test_the_prompt_names_the_volumes_and_does_not_claim_only_files(tmp_path: Path) -> None:
    prefix = _project(tmp_path)
    leftover = f"{prefix}_old-cache"
    removed: list[str] = []
    result = _cleanup(tmp_path, [leftover], ["--docker"], removed, input="n\n")
    out = _out(result)
    assert "Docker volume" in out.split("Delete", 1)[1]
    assert "of files?" not in out
    assert removed == []


@pytest.mark.unit
def test_an_unreadable_compose_file_removes_no_volume(tmp_path: Path) -> None:
    prefix = get_compose_project_name(tmp_path)
    (tmp_path / "docker-compose.yml").write_text("volumes: [unclosed", encoding="utf-8")
    removed: list[str] = []
    result = _cleanup(tmp_path, [f"{prefix}_old-cache"], ["--docker", "--yes"], removed)
    assert removed == []
    assert "compose" in _out(result).lower()


def _audit_docker(containers: str, volume: str, project_id: str | None):
    labels = {"com.dango.project_id": project_id} if project_id else None

    def run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        if argv[:2] == ["docker", "ps"]:
            return MagicMock(returncode=0, stdout=containers)
        if argv[:3] == ["docker", "volume", "ls"]:
            return MagicMock(returncode=0, stdout=f"{volume}\n")
        if argv[:3] == ["docker", "volume", "inspect"]:
            return MagicMock(returncode=0, stdout=json.dumps([{"Name": volume, "Labels": labels}]))
        return MagicMock(returncode=0, stdout="")

    return run


def _audit(tmp_path: Path, project: str, project_id: str | None, current: Path | None):
    moved = str(tmp_path / "old-location")  # never created: the project directory moved
    line = f"abc\t{project}-metabase-1\t{project}\t{moved}\n"
    finder = MagicMock(return_value=current) if current else MagicMock(side_effect=Exception("x"))
    with (
        patch(
            "subprocess.run",
            side_effect=_audit_docker(line, f"{project}_metabase-data", project_id),
        ),
        patch("dango.cli.utils.find_project_root", finder),
    ):
        return CliRunner().invoke(docker_audit, ["--dry-run"])


@pytest.mark.unit
def test_the_current_projects_own_group_is_never_orphaned(tmp_path: Path) -> None:
    project = get_compose_project_name(tmp_path)
    result = _audit(tmp_path, project, None, tmp_path)
    out = _out(result)
    assert "Orphaned" not in out
    assert "orphaned" not in out.lower().replace("no unambiguously orphaned", "")


@pytest.mark.unit
def test_a_volume_carrying_a_project_id_is_never_safe_to_clean(tmp_path: Path) -> None:
    result = _audit(tmp_path, "dango-aaaa1111", "aaaa1111deadbeef", None)
    out = _out(result)
    assert "Orphaned" not in out
    assert "1 group(s) need attention" in out
