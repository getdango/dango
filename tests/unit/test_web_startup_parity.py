"""tests/unit/test_web_startup_parity.py

`dango web` runs the same local startup prologue as `dango serve` before it serves (project .env,
DuckDB version alignment, log rotation, pending migrations, dbt schemas) and stays out of Docker.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from click.testing import CliRunner, Result

from dango.cli.commands.serve import serve
from dango.cli.main import cli
from dango.exceptions import DbtLockError, MigrationError, VersionMismatchError
from dango.utils.dbt_lock import DbtLock
from tests.factories.scaffold_project import build_fresh_project, working_copy

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_STARTUP = "dango.platform.common.startup"
_SERVE = "dango.cli.commands.serve"
_ENV_VARS = ("WP32_SECRET", "DANGO_ADMIN_EMAIL", "DANGO_PROJECT_ROOT")
_SCHEMAS = {"raw", "staging", "intermediate", "marts"}
_SECRET = "hunter2-secret-value"


@pytest.fixture(scope="module")
def fresh_project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return build_fresh_project(tmp_path_factory)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("DANGO_TELEMETRY", "0")
    for name in _ENV_VARS:  # load_dotenv and the commands set these: restore on teardown
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


@pytest.fixture
def project(fresh_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = working_copy(fresh_project, tmp_path)
    monkeypatch.chdir(root)
    return root


class _Served:
    """What the process looked like at the moment the server would have started."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root
        self.called = 0
        self.env: dict[str, str | None] = {}
        self.auth_tables: set[str] = set()
        self.schemas: set[str] = set()
        self.audit_bytes = -1
        self.lock_free = False

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.called += 1
        self.env = {name: os.environ.get(name) for name in _ENV_VARS}
        auth = self.root / ".dango" / "auth.db"
        if auth.exists():
            with sqlite3.connect(auth) as conn:
                rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            self.auth_tables = {row[0] for row in rows}
        warehouse = self.root / "data" / "warehouse.duckdb"
        if warehouse.exists():
            conn2 = duckdb.connect(str(warehouse), read_only=True)
            try:
                found = conn2.execute("SELECT schema_name FROM information_schema.schemata")
                self.schemas = {row[0] for row in found.fetchall()}
            finally:
                conn2.close()
        audit = self.root / ".dango" / "logs" / "audit.jsonl"
        self.audit_bytes = audit.stat().st_size if audit.exists() else -1


def _web(project: Path, *args: str) -> tuple[Result, _Served]:
    served = _Served(project)
    with patch("uvicorn.run", side_effect=served):
        result = CliRunner().invoke(cli, ["web", *args])
    return result, served


def _text(result: Result) -> str:
    return _ANSI.sub("", result.output)


# --- the prologue runs before the server starts ---------------------------------------------


def test_web_loads_the_project_env_file(project: Path) -> None:
    (project / ".env").write_text(
        f"WP32_SECRET={_SECRET}\nDANGO_ADMIN_EMAIL=owner@example.com\n", encoding="utf-8"
    )
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert served.env["WP32_SECRET"] == _SECRET
    assert served.env["DANGO_ADMIN_EMAIL"] == "owner@example.com"


def test_web_does_not_override_the_real_environment_with_the_env_file(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WP32_SECRET", "from-the-shell")
    (project / ".env").write_text("WP32_SECRET=from-the-file\n", encoding="utf-8")
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert served.env["WP32_SECRET"] == "from-the-shell"


def test_web_exports_the_project_root_for_audit_logging(project: Path) -> None:
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert served.env["DANGO_PROJECT_ROOT"] == str(project)


def test_web_leaves_no_project_root_variable_behind_after_it_stops(project: Path) -> None:
    result, _served = _web(project)
    assert result.exit_code == 0, result.output
    assert "DANGO_PROJECT_ROOT" not in os.environ


def test_web_applies_pending_migrations_to_a_cloned_project(project: Path) -> None:
    for name in ("auth.db", "scheduler.db"):
        (project / ".dango" / name).unlink()
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert {"users", "login_attempts", "_migrations"} <= served.auth_tables


def test_web_creates_the_dbt_schemas_in_the_warehouse(project: Path) -> None:
    (project / "data" / "warehouse.duckdb").unlink()
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert _SCHEMAS <= served.schemas


def test_web_rotates_an_oversized_audit_log(project: Path) -> None:
    log = project / ".dango" / "logs" / "audit.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("x" * (6 * 1024 * 1024), encoding="utf-8")
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert served.audit_bytes < 1024 * 1024
    assert list(log.parent.glob("audit.*.gz"))


def test_a_lock_file_left_by_a_crashed_process_does_not_block_web(project: Path) -> None:
    state = project / ".dango" / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "dbt.lock").write_text("", encoding="utf-8")
    (state / "dbt.lock.json").write_text('{"pid": 999999, "source": "sync"}', encoding="utf-8")
    result, served = _web(project)
    assert result.exit_code == 0, result.output
    assert served.called == 1
    lock = DbtLock(project, source="test", operation="after web")
    assert lock.acquire(timeout=0)
    lock.release()


# --- order and the single-writer lock -------------------------------------------------------


def _recorders(events: list[str]) -> list[Any]:
    def rec(name: str, result: Any = None) -> Callable[..., Any]:
        def fake(*_a: Any, **_k: Any) -> Any:
            events.append(name)
            return result

        return fake

    return [
        patch(f"{_STARTUP}.check_duckdb_version_alignment", side_effect=rec("align")),
        patch(f"{_STARTUP}.rotate_logs", side_effect=rec("rotate")),
        patch(f"{_STARTUP}.run_pending_migrations", side_effect=rec("migrate", {})),
        patch(f"{_STARTUP}.cleanup_stale_dbt_lock", side_effect=rec("stale_lock", False)),
        patch(f"{_STARTUP}.ensure_dbt_schemas", side_effect=rec("schemas")),
    ]


def _run_recorded(command: str, project: Path) -> list[str]:
    events: list[str] = []
    patches = _recorders(events)
    patches.append(patch("dotenv.load_dotenv", side_effect=lambda *_a, **_k: events.append("env")))
    patches.append(patch("uvicorn.run", side_effect=lambda *_a, **_k: events.append("uvicorn")))
    if command == "serve":
        patches += [
            patch(f"{_SERVE}._stop_docker_quiet", side_effect=lambda _r: events.append("stop")),
            patch(f"{_SERVE}._check_port"),
            patch(f"{_STARTUP}.ensure_duckdb_driver", side_effect=lambda _r: events.append("drv")),
            patch(f"{_STARTUP}.start_docker_services", side_effect=lambda _r: events.append("up")),
            patch(f"{_STARTUP}.wait_for_metabase_if_needed", return_value=None),
            patch(f"{_STARTUP}.setup_metabase_if_needed", return_value={"success": True}),
            patch(f"{_STARTUP}.apply_metabase_duckdb_lock_if_needed"),
            patch(f"{_STARTUP}.import_dashboards"),
            patch(
                "dango.platform.common.metabase_credential_migration."
                "complete_metabase_credential_migration",
                return_value={"status": "none"},
            ),
        ]
    for p in patches:
        p.start()
    try:
        result = CliRunner().invoke(
            serve if command == "serve" else cli, [] if command == "serve" else ["web"]
        )
    finally:
        for p in reversed(patches):
            p.stop()
    assert result.exit_code == 0, result.output
    return events


def test_web_runs_the_prologue_steps_in_serve_order(project: Path) -> None:
    assert _run_recorded("web", project) == [
        "env",
        "align",
        "rotate",
        "migrate",
        "stale_lock",
        "schemas",
        "uvicorn",
    ]


def test_serve_step_order_is_unchanged(project: Path) -> None:
    assert _run_recorded("serve", project) == [
        "env",
        "align",
        "rotate",
        "migrate",
        "stale_lock",
        "stop",
        "schemas",
        "drv",
        "up",
        "uvicorn",
        "stop",
    ]


def test_web_holds_the_dbt_lock_while_it_writes_the_schemas(project: Path) -> None:
    held: list[bool] = []

    def probe(_root: Path) -> None:
        try:
            DbtLock(project, source="probe", operation="probe").acquire(timeout=0)
            held.append(False)
        except DbtLockError:
            held.append(True)

    with patch(f"{_STARTUP}.ensure_dbt_schemas", side_effect=probe):
        result, _served = _web(project)
    assert result.exit_code == 0, result.output
    assert held == [True]
    lock = DbtLock(project, source="test", operation="after")
    assert lock.acquire(timeout=0), "the lock must be released before the server starts"
    lock.release()


def test_web_aborts_when_another_process_holds_the_dbt_lock(project: Path) -> None:
    holder = DbtLock(project, source="sync", operation="load")
    assert holder.acquire(timeout=0)
    try:
        with patch.object(DbtLock, "acquire", side_effect=DbtLockError("lock busy: boom")):
            result, served = _web(project)
    finally:
        holder.release()
    assert result.exit_code == 1
    assert served.called == 0
    assert "lock busy: boom" in _text(result)


# --- failures in web: one clear message, Abort, never a traceback --------------------------

_FAILURES = [
    (
        "check_duckdb_version_alignment",
        VersionMismatchError("driver pin differs"),
        "DuckDB version mismatch",
    ),
    ("run_pending_migrations", MigrationError("cannot migrate"), "Migration failed"),
    ("ensure_dbt_schemas", RuntimeError("cannot create"), "Schema setup failed"),
]


@pytest.mark.parametrize(("target", "error", "label"), _FAILURES)
def test_web_failing_step_prints_the_reason_and_aborts(
    project: Path, target: str, error: Exception, label: str
) -> None:
    with patch(f"{_STARTUP}.{target}", side_effect=error):
        result, served = _web(project)
    output = _text(result)
    assert result.exit_code == 1
    assert served.called == 0
    assert label in output and str(error) in output
    assert "Traceback" not in output
    assert output.count("Error starting server") == 1
    assert "Aborted" in output


def test_web_does_not_touch_duckdb_or_migrate_after_a_version_mismatch(project: Path) -> None:
    (project / "data" / "warehouse.duckdb").unlink()
    with patch("dango.utils.driver.METABASE_DUCKDB_DRIVER_VERSION", "1.0.0.0"):
        result, served = _web(project)
    assert result.exit_code == 1
    assert served.called == 0
    assert "DuckDB version mismatch" in _text(result)
    assert not (project / "data" / "warehouse.duckdb").exists()


def test_web_failure_text_is_redacted_and_not_parsed_as_markup(project: Path) -> None:
    (project / ".env").write_text(f"WP32_SECRET={_SECRET}\n", encoding="utf-8")
    error = RuntimeError(f"cannot create [bold]x[/bold] password={_SECRET}")
    with patch(f"{_STARTUP}.ensure_dbt_schemas", side_effect=error):
        result, _served = _web(project)
    output = _text(result)
    assert result.exit_code == 1
    assert _SECRET not in output
    assert "[bold]x[/bold]" in output


@pytest.mark.parametrize(("target", "error", "label"), _FAILURES)
def test_serve_failure_output_is_unchanged(
    project: Path, target: str, error: Exception, label: str
) -> None:
    runner = CliRunner()
    with (
        patch(f"{_STARTUP}.{target}", side_effect=error),
        patch(f"{_SERVE}._stop_docker_quiet"),
        patch("uvicorn.run") as run,
    ):
        result = runner.invoke(serve, [])
    assert result.exit_code == 1
    assert result.stderr == f"{label}: {error}\n"
    run.assert_not_called()


# --- what web deliberately does not do ------------------------------------------------------


def test_web_does_not_start_docker_metabase_or_download_the_driver(project: Path) -> None:
    names = (
        "start_docker_services",
        "ensure_duckdb_driver",
        "setup_metabase_if_needed",
        "import_dashboards",
    )
    patches = {name: patch(f"{_STARTUP}.{name}", MagicMock()) for name in names}
    mocks = {name: p.start() for name, p in patches.items()}
    try:
        with patch("dango.platform.DockerManager") as docker:
            result, served = _web(project)
    finally:
        for p in patches.values():
            p.stop()
    assert result.exit_code == 0, result.output
    assert served.called == 1
    docker.assert_not_called()
    for name, mock in mocks.items():
        assert not mock.called, name


def test_web_default_bind_is_unchanged(project: Path) -> None:
    with patch("uvicorn.run") as run:
        result = CliRunner().invoke(cli, ["web"])
    assert result.exit_code == 0, result.output
    assert run.call_args.kwargs["host"] == "0.0.0.0"
    assert run.call_args.kwargs["port"] == 8080


def test_web_help_says_what_it_does_not_do() -> None:
    text = " ".join(_text(CliRunner().invoke(cli, ["web", "--help"])).split())
    assert "dango start" in text
    assert "dango serve" in text
    assert "Docker" in text and "Metabase" in text
