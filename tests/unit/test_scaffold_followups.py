"""tests/unit/test_scaffold_followups.py

Scaffold check edge cases: dry-run, failed restores, prompt output, ordering and the advisory macro row.
"""

from __future__ import annotations

import builtins
import re
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.main import cli
from dango.cli.validate import ProjectValidator
from tests.factories.scaffold_project import (
    build_fresh_project,
    tree,
    working_copy,
)

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
MACRO = "dbt/macros/get_custom_schema.sql"


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = working_copy(fresh_project, tmp_path)
    monkeypatch.chdir(root)
    return root


def _invoke(argv: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli, argv)
    return result.exit_code, _ANSI.sub("", result.output)


def _sync(*extra: str) -> tuple[int, str]:
    with patch(
        "dango.visualization.metabase.refresh_metabase_connection",
        return_value=(False, None, None),
    ):
        return _invoke(["sync", "--yes", *extra])


class _Docker:
    def __init__(self) -> None:
        self.services = MagicMock(side_effect=RuntimeError("services stopped by the test"))

    def __enter__(self) -> _Docker:
        def no_docker(*args: Any, **kwargs: Any) -> Any:
            raise FileNotFoundError("docker is not run by these tests")

        self._patches = [
            patch("dango.platform.common.startup.start_docker_services", self.services),
            patch("dango.platform.common.startup.ensure_duckdb_driver", MagicMock()),
            patch("subprocess.run", no_docker),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in self._patches:
            p.stop()


# --- sync --dry-run --------------------------------------------------------------------


def test_dry_run_of_a_csv_source_prints_its_directory(project: Path) -> None:
    code, output = _sync("--dry-run")
    assert code == 0, output
    assert "Path:" in output and "inputs" in output
    assert "AttributeError" not in output and "file_path" not in output


def test_dry_run_only_reports_a_broken_scaffold(project: Path) -> None:
    for name in ("dbt/profiles.yml", "dbt/dbt_project.yml"):
        (project / name).unlink()
    shutil.rmtree(project / "data")
    shutil.rmtree(project / ".dlt")
    before = tree(project)
    code, output = _sync("--dry-run")
    assert code == 0, output
    assert tree(project) == before
    for name in ("dbt/profiles.yml", "dbt/dbt_project.yml"):
        assert name in output


def test_dry_run_with_no_enabled_sources_keeps_its_exit_code(project: Path) -> None:
    (project / ".dango" / "sources.yml").write_text(
        "version: '1.0'\nsources: []\n", encoding="utf-8"
    )
    (project / "dbt" / "profiles.yml").unlink()
    code, output = _sync("--dry-run")
    assert code == 0, output
    assert not (project / "data" / "never").exists()


# --- failed restores -------------------------------------------------------------------


def _deny_creating(name: str) -> Any:
    real_open = builtins.open

    def fake_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if Path(str(file)).name == name and "x" in mode:
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, mode, *args, **kwargs)

    return patch("builtins.open", fake_open)


def test_start_yes_reports_a_restore_that_is_not_allowed(project: Path) -> None:
    (project / "Dockerfile.metabase").unlink()
    with _Docker() as docker, _deny_creating("Dockerfile.metabase"):
        code, output = _invoke(["start", "--yes"])
    assert code == 1, output
    assert "Dockerfile.metabase" in output and "Permission denied" in output
    assert "Stopping all services" not in output
    assert "Unexpected error" not in output
    assert "Traceback" not in output
    assert docker.services.call_count == 0


def test_sync_reports_a_dlt_directory_that_cannot_be_created(project: Path) -> None:
    shutil.rmtree(project / ".dlt")
    with patch(
        "dango.config.credentials.init_dlt_directory",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        code, output = _sync()
    assert code == 1, output
    assert ".dlt" in output and "Permission denied" in output
    assert "Traceback" not in output


# --- prompt output and ordering --------------------------------------------------------


def test_declined_restore_prints_the_error_on_its_own_line(project: Path) -> None:
    (project / "entrypoint.sh").unlink()
    with _Docker():
        code, output = _invoke(["start"])
    assert code == 1, output
    for line in output.splitlines():
        assert not ("(yes/no)" in line and "Error:" in line), line


def test_safe_confirm_docstring_does_not_claim_it_skips_the_prompt() -> None:
    from dango.cli.utils import safe_confirm

    assert "returned without\n    prompting" not in (safe_confirm.__doc__ or "")


def test_nothing_is_created_before_a_blocking_problem_aborts(project: Path) -> None:
    shutil.rmtree(project / "data")
    shutil.rmtree(project / ".dlt")
    (project / "dbt" / "profiles.yml").unlink()
    before = tree(project)
    code, output = _sync()
    assert code == 1, output
    assert tree(project) == before
    assert not (project / "data").exists() and not (project / ".dlt").exists()


# --- the advisory macro row -------------------------------------------------------------


def _rows(project: Path) -> dict[str, tuple[str, str]]:
    summary = ProjectValidator(project).validate_all(display=False, include_connectivity=False)
    return {r.name: (r.status, r.message) for r in summary["results"]}


@pytest.fixture
def no_dbt_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProjectValidator, "_validate_dbt_models", lambda self: None)


@pytest.mark.usefixtures("no_dbt_parse")
def test_validate_warns_when_the_schema_macro_is_missing(project: Path) -> None:
    assert _rows(project)[f"Scaffold: {MACRO}"][0] == "pass"
    (project / MACRO).unlink()
    status, message = _rows(project)[f"Scaffold: {MACRO}"]
    assert status == "warn"
    assert "main_staging" in message
    assert not (project / MACRO).exists()


@pytest.mark.parametrize("command", ["start", "sync", "run"])
def test_the_schema_macro_is_not_enforced_by_the_commands(project: Path, command: str) -> None:
    from dango.cli.scaffold import check_scaffold

    (project / MACRO).unlink()
    assert check_scaffold(project, command).missing == []


def test_inventory_says_what_project_and_sources_entries_do() -> None:
    from dango.cli.scaffold import SCAFFOLD_ITEMS

    actions = {i.path: i.action for i in SCAFFOLD_ITEMS}
    assert actions[".dango/project.yml"] == "marker"
    assert actions[".dango/sources.yml"] == "optional"
    assert actions[MACRO] == "advise"
