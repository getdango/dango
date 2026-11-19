"""tests/unit/test_scaffold_validation.py

`dango start`, `sync`, `run` and `validate` report missing scaffold files with the fix, never a raw tool error.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from dango.cli.init import ProjectInitializer
from dango.cli.main import cli
from dango.cli.validate import ProjectValidator

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_BLANK_ERROR = re.compile(r"^\s*Error:\s*$", re.MULTILINE)
RAW_TOOL_TEXT = (
    "failed to read dockerfile",
    "Traceback",
    "FileNotFoundError",
    "Runtime Error",
    "Could not find profile",
    "No dbt_project.yml found",
    "dbt build failed",
)
ALL_COMMANDS = ("start", "sync", "run", "validate", "web")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One project built by the real `init --skip-wizard`, reused as a template."""
    home = tmp_path_factory.mktemp("home")
    root = tmp_path_factory.mktemp("fresh") / "proj"
    patcher = pytest.MonkeyPatch()
    patcher.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    patcher.setenv("HOME", str(home))
    patcher.setenv("USERPROFILE", str(home))
    patcher.setenv("DANGO_TELEMETRY", "0")
    patcher.setattr("dango.utils.driver.reuse_verified_driver", lambda plugins_dir: True)
    patcher.setattr(ProjectInitializer, "_generate_dbt_docs", lambda self: True)
    try:
        ProjectInitializer(root).initialize(skip_wizard=True)
    finally:
        patcher.undo()
    return root


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private copy with free ports and one CSV source, as the current directory."""
    root = tmp_path / "proj"
    shutil.copytree(fresh_project, root)
    project_yml = root / ".dango" / "project.yml"
    data = yaml.safe_load(project_yml.read_text(encoding="utf-8"))
    data["platform"] = {
        "port": _free_port(),
        "metabase_port": _free_port(),
        "dbt_docs_port": _free_port(),
        "marimo_port": _free_port(),
    }
    project_yml.write_text(yaml.safe_dump(data), encoding="utf-8")
    (root / ".dango" / "sources.yml").write_text(
        "version: '1.0'\nsources:\n  - name: demo\n    type: csv\n    csv:\n      directory: inputs\n",
        encoding="utf-8",
    )
    (root / "inputs").mkdir()
    (root / "inputs" / "a.csv").write_text("id,v\n1,2\n3,4\n", encoding="utf-8")
    monkeypatch.chdir(root)
    return root


def _tree(root: Path) -> dict[str, str]:
    """Relative path -> content hash (or 'dir'), ignoring the CLI's own log files."""
    found: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".dango/logs"):
            continue
        found[rel] = "dir" if path.is_dir() else hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def _remove(path: Path) -> None:
    shutil.rmtree(path) if path.is_dir() else path.unlink()


class _Docker:
    """Stand-ins that keep `start` away from Docker and the network."""

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


def _invoke(argv: list[str]) -> tuple[int, str]:
    result = CliRunner().invoke(cli, argv)
    return result.exit_code, _ANSI.sub("", result.output)


def _assert_clean_failure(code: int, output: str, *names: str) -> None:
    assert code == 1, output
    for name in names:
        assert name in output, output
    for text in RAW_TOOL_TEXT:
        assert text not in output, output
    assert not _BLANK_ERROR.search(output), output


def _sync(*extra: str) -> tuple[int, str]:
    with patch(
        "dango.visualization.metabase.refresh_metabase_connection",
        return_value=(False, None, None),
    ):
        return _invoke(["sync", "--yes", *extra])


# --- start -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Dockerfile.metabase", "entrypoint.sh"])
def test_start_without_template_file_names_it_and_the_fix(project: Path, name: str) -> None:
    _remove(project / name)
    before = _tree(project)
    with _Docker() as docker:
        code, output = _invoke(["start"])
    _assert_clean_failure(code, output, name, "dango start --yes")
    assert docker.services.call_count == 0
    assert _tree(project) == before


@pytest.mark.parametrize("name", ["Dockerfile.metabase", "entrypoint.sh"])
def test_start_yes_recreates_the_file_byte_identical_to_init(
    project: Path, fresh_project: Path, name: str
) -> None:
    _remove(project / name)
    with _Docker() as docker:
        _invoke(["start", "--yes"])
    assert (project / name).read_bytes() == (fresh_project / name).read_bytes()
    assert docker.services.call_count == 1


def test_start_yes_recreates_entrypoint_executable(project: Path) -> None:
    (project / "entrypoint.sh").unlink()
    with _Docker():
        _invoke(["start", "--yes"])
    assert os.access(project / "entrypoint.sh", os.X_OK) or sys.platform == "win32"


@pytest.mark.parametrize("name", ["Dockerfile.metabase", "entrypoint.sh"])
def test_start_never_overwrites_an_existing_user_file(project: Path, name: str) -> None:
    mine = b"# my own edit \xe2\x9c\x93\nENV MB_VERSION=v0.1.2\n"
    (project / name).write_bytes(mine)
    with _Docker() as docker:
        _invoke(["start", "--yes"])
    assert (project / name).read_bytes() == mine
    assert docker.services.call_count == 1


def test_start_confirmation_decides_between_recreate_and_abort(project: Path) -> None:
    (project / "Dockerfile.metabase").unlink()
    with _Docker() as docker, patch("dango.cli.utils.safe_confirm", return_value=False):
        code, output = _invoke(["start"])
    _assert_clean_failure(code, output, "Dockerfile.metabase")
    assert docker.services.call_count == 0
    assert not (project / "Dockerfile.metabase").exists()
    with _Docker() as docker, patch("dango.cli.utils.safe_confirm", return_value=True):
        _invoke(["start"])
    assert (project / "Dockerfile.metabase").is_file()
    assert docker.services.call_count == 1


@pytest.mark.parametrize("name", ["Dockerfile.metabase", "entrypoint.sh"])
def test_start_with_unusable_file_is_reported_and_left_alone(project: Path, name: str) -> None:
    _remove(project / name)
    (project / name).mkdir()
    before = _tree(project)
    with _Docker() as docker:
        code, output = _invoke(["start", "--yes"])
    _assert_clean_failure(code, output, name, "not a regular file")
    assert docker.services.call_count == 0
    assert _tree(project) == before


def test_start_recreates_a_missing_data_directory_silently(project: Path) -> None:
    shutil.rmtree(project / "data")
    with _Docker() as docker:
        _invoke(["start"])
    assert (project / "data").is_dir()
    assert docker.services.call_count == 1


def test_start_on_a_healthy_project_reaches_docker_with_no_scaffold_message(
    project: Path,
) -> None:
    with _Docker() as docker:
        _, output = _invoke(["start"])
    assert docker.services.call_count == 1
    assert "missing" not in output.lower()


def test_web_does_not_demand_docker_files(project: Path) -> None:
    (project / "Dockerfile.metabase").unlink()
    (project / "entrypoint.sh").unlink()
    with patch("uvicorn.run") as server:
        code, output = _invoke(["web"])
    assert code == 0, output
    assert server.call_count == 1
    assert "Dockerfile.metabase" not in output


# --- sync and run ----------------------------------------------------------------------


@pytest.mark.parametrize("name", ["dbt/dbt_project.yml", "dbt/profiles.yml"])
def test_sync_without_dbt_files_aborts_before_loading_anything(project: Path, name: str) -> None:
    _remove(project / name)
    before = _tree(project)
    code, output = _sync()
    _assert_clean_failure(code, output, name)
    assert "version control" in output
    assert _tree(project) == before


def test_sync_dry_run_reports_a_broken_scaffold_without_failing(project: Path) -> None:
    (project / "dbt" / "profiles.yml").unlink()
    code, output = _sync("--dry-run")
    assert code == 0, output
    assert "dbt/profiles.yml" in output
    for text in RAW_TOOL_TEXT:
        assert text not in output


def test_sync_recreates_a_missing_data_directory_and_succeeds(project: Path) -> None:
    shutil.rmtree(project / "data")
    code, output = _sync()
    assert code == 0, output
    assert (project / "data" / "warehouse.duckdb").is_file()


def test_sync_recreates_a_missing_dlt_directory_and_succeeds(project: Path) -> None:
    shutil.rmtree(project / ".dlt")
    code, output = _sync()
    assert code == 0, output
    assert (project / ".dlt" / "secrets.toml").is_file()
    assert (project / ".dlt" / "config.toml").is_file()


def test_sync_on_a_healthy_project_succeeds(project: Path) -> None:
    code, output = _sync()
    assert code == 0, output
    assert "missing" not in output.lower()


@pytest.mark.parametrize("name", ["dbt/dbt_project.yml", "dbt/profiles.yml"])
def test_run_without_dbt_files_names_them_and_does_not_call_dbt(project: Path, name: str) -> None:
    _remove(project / name)
    before = _tree(project)
    code, output = _invoke(["run"])
    _assert_clean_failure(code, output, name)
    assert _tree(project) == before


# --- validate --------------------------------------------------------------------------


def _validate_rows(project: Path) -> dict[str, tuple[str, str]]:
    summary = ProjectValidator(project).validate_all(display=False, include_connectivity=False)
    return {r.name: (r.status, r.message) for r in summary["results"]}


@pytest.fixture
def no_dbt_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ProjectValidator, "_validate_dbt_models", lambda self: None)


@pytest.mark.usefixtures("no_dbt_parse")
@pytest.mark.parametrize("name", ["docker-compose.yml", "Dockerfile.metabase", "entrypoint.sh"])
def test_validate_warns_with_the_fix_for_a_missing_docker_file(project: Path, name: str) -> None:
    _remove(project / name)
    status, message = _validate_rows(project)[f"Scaffold: {name}"]
    assert status == "warn"
    assert "dango start" in message


@pytest.mark.usefixtures("no_dbt_parse")
def test_validate_warns_for_a_missing_dlt_directory(project: Path) -> None:
    shutil.rmtree(project / ".dlt")
    status, message = _validate_rows(project)["Scaffold: .dlt"]
    assert status == "warn"
    assert "dango sync" in message


@pytest.mark.usefixtures("no_dbt_parse")
def test_validate_fails_for_an_unusable_docker_file(project: Path) -> None:
    (project / "entrypoint.sh").unlink()
    (project / "entrypoint.sh").mkdir()
    status, message = _validate_rows(project)["Scaffold: entrypoint.sh"]
    assert status == "fail"
    assert "not a regular file" in message


@pytest.mark.usefixtures("no_dbt_parse")
def test_validate_healthy_project_only_gains_passing_scaffold_rows(project: Path) -> None:
    rows = _validate_rows(project)
    scaffold = {n: v for n, v in rows.items() if n.startswith("Scaffold: ")}
    assert set(scaffold) == {
        "Scaffold: docker-compose.yml",
        "Scaffold: Dockerfile.metabase",
        "Scaffold: entrypoint.sh",
        "Scaffold: .dlt",
        "Scaffold: dbt/macros/get_custom_schema.sql",
    }
    assert {v[0] for v in scaffold.values()} == {"pass"}
    with patch("dango.cli.scaffold.check_scaffold") as check:
        check.return_value.missing = []
        check.return_value.unreadable = []
        without = _validate_rows(project)
    assert {n: v for n, v in rows.items() if n not in scaffold} == {
        n: v for n, v in without.items() if not n.startswith("Scaffold: ")
    }


@pytest.mark.parametrize("name", ["dbt/dbt_project.yml", "dbt/profiles.yml"])
def test_validate_does_not_pass_a_raw_dbt_error_through(project: Path, name: str) -> None:
    _remove(project / name)
    rows = _validate_rows(project)
    assert rows[f"dbt: {name.split('/')[1]}"][0] == "fail"
    status, message = rows["dbt: Model validation"]
    assert status == "fail"
    assert "skipped" in message.lower()
    for text in RAW_TOOL_TEXT:
        assert text not in message


def test_validate_command_exit_codes_follow_warn_and_fail(project: Path) -> None:
    (project / "Dockerfile.metabase").unlink()
    with patch.object(ProjectValidator, "_validate_dbt_models", lambda self: None):
        code, output = _invoke(["validate"])
    assert code == 0, output
    assert "Dockerfile.metabase" in output
    (project / "entrypoint.sh").unlink()
    (project / "entrypoint.sh").mkdir()
    with patch.object(ProjectValidator, "_validate_dbt_models", lambda self: None):
        code, output = _invoke(["validate"])
    assert code == 1, output


# --- the validator itself --------------------------------------------------------------


def _items() -> tuple[Any, ...]:
    from dango.cli.scaffold import SCAFFOLD_ITEMS

    return tuple(SCAFFOLD_ITEMS)


def test_inventory_lists_every_file_the_commands_need_and_init_creates_them(
    fresh_project: Path,
) -> None:
    items = {item.path: item for item in _items()}
    assert {
        ".dango/project.yml",
        ".dango/sources.yml",
        "docker-compose.yml",
        "Dockerfile.metabase",
        "entrypoint.sh",
        "dbt/dbt_project.yml",
        "dbt/profiles.yml",
        "data",
        ".dlt",
    } <= set(items)
    for path in items:
        assert (fresh_project / path).exists(), path


def test_inventory_top_level_names_are_ones_init_rollback_knows() -> None:
    from dango.cli.init_rollback import INIT_TOP_LEVEL

    for item in _items():
        assert item.path.split("/")[0] in INIT_TOP_LEVEL, item.path


@pytest.mark.parametrize("command", ALL_COMMANDS)
def test_check_scaffold_reports_each_item_only_for_the_commands_that_need_it(
    project: Path, command: str
) -> None:
    from dango.cli.scaffold import check_scaffold

    for item in _items():
        target = project / item.path
        saved = tmp = None
        if target.is_dir():
            saved = project.parent / "saved_dir"
            shutil.move(str(target), str(saved))
        else:
            tmp = target.read_bytes()
            target.unlink()
        before = _tree(project)
        report = check_scaffold(project, command)
        assert _tree(project) == before, "check_scaffold must not write"
        reported = {m.item.path for m in report.missing}
        assert (item.path in reported) == (command in item.commands), (item.path, command)
        if saved is not None:
            shutil.move(str(saved), str(target))
        else:
            assert tmp is not None
            target.write_bytes(tmp)


def test_every_message_has_the_fix_and_is_ascii(project: Path) -> None:
    from dango.cli.scaffold import check_scaffold

    for item in _items():
        target = project / item.path
        if target.is_dir():
            continue
        data = target.read_bytes()
        target.unlink()
        report = check_scaffold(project, "validate")
        target.write_bytes(data)
        for missing in report.missing:
            text = missing.message
            assert item.path in text
            assert text.isascii()
            assert any(w in text for w in ("dango start", "version control", "backup")), text
            assert missing.regenerable == (item.action in ("restore", "render", "auto"))


def test_a_missing_regenerable_file_is_never_reported_as_blocking_for_validate(
    project: Path,
) -> None:
    from dango.cli.scaffold import check_scaffold

    (project / "Dockerfile.metabase").unlink()
    report = check_scaffold(project, "validate")
    assert [m.regenerable for m in report.missing] == [True]
    assert report.unreadable == []


@pytest.mark.parametrize("name", ["Dockerfile.metabase", "entrypoint.sh"])
def test_restore_never_overwrites_a_file_that_appeared_after_the_check(
    project: Path, name: str
) -> None:
    from dango.cli.scaffold import ScaffoldProblem, restore_scaffold

    item = next(i for i in _items() if i.path == name)
    mine = b"# written between the check and the restore\n"
    (project / name).write_bytes(mine)
    lines = restore_scaffold(project, [ScaffoldProblem(item, "x", True)])
    assert (project / name).read_bytes() == mine
    assert lines == []
