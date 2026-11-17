"""tests/unit/test_runner_error_text.py

Error text printed by the dlt runner is redacted and escaped (secrets, Rich markup).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import pytest
import requests
from rich.console import Console

from dango.config.models import SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner, run_sync

# dango.utils exports a function named like some submodules; use the real module for patching
_db_health = importlib.import_module("dango.utils.db_health")

pytestmark = pytest.mark.unit

TOML_SECRET = "TomlSecretValue987"
URL_ERROR = (
    "401 Client Error: Unauthorized for url: https://api.example.com/v1/x?api_key=SECRET123&page=2"
)
DB_ERROR = (
    "(psycopg2.OperationalError) could not connect to postgresql://dbuser:PASSw0rd9@dbhost:5432/db"
)
MARKUP_ERROR = f"boom [/x] [red]oops[/red] [foo] token={TOML_SECRET}"
SECRETS = ("SECRET123", "PASSw0rd9", TOML_SECRET)

ERRORS = {
    "requests_url": requests.HTTPError(URL_ERROR),
    "sqlalchemy_url": RuntimeError(DB_ERROR),
    "markup_and_toml": RuntimeError(MARKUP_ERROR),
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".dlt").mkdir()
    (tmp_path / ".dlt" / "secrets.toml").write_text(
        f'[sources.custom]\nclient_secret = "{TOML_SECRET}"\n', encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def out() -> Iterator[StringIO]:
    buf = StringIO()
    with patch("dango.ingestion.dlt_runner.console", Console(file=buf, width=2000, markup=True)):
        yield buf


def _source(name: str, kind: str) -> MagicMock:
    src = MagicMock()
    src.name = name
    src.enabled = True
    src.empty_sync_policy = "block"
    if kind == "native":
        src.type = SourceType.DLT_NATIVE
        src.dlt_native.source_module = "fake_mod"
        src.dlt_native.source_function = "fake_fn"
        src.dlt_native.function_kwargs = {}
        src.dlt_native.dataset_name = None
        src.dlt_native.pipeline_name = name
    else:
        src.type = SourceType.HUBSPOT
    return src


def _failing_pipeline(exc: Exception) -> MagicMock:
    pipeline = MagicMock(spec=dlt.Pipeline)
    pipeline.extract.side_effect = exc
    pipeline.dataset_name = "raw_x"
    return pipeline


def _run(
    project: Path,
    kind: str,
    exc: Exception,
    names: tuple[str, ...],
    *,
    pipeline_extra: dict[str, Any] | None = None,
    health: Any = None,
    full_refresh: bool = False,
) -> dict[str, Any]:
    """Run ``run_sync`` over failing sources of one kind with extract raising *exc*."""
    pipeline = _failing_pipeline(exc)
    for attr, value in (pipeline_extra or {}).items():
        getattr(pipeline, attr).side_effect = value
    module = MagicMock()
    module.fake_fn.return_value = MagicMock()
    meta = {"dlt_package": "dlt.sources.test", "dlt_function": "fake_fn"}
    runner_patches = {
        "_check_dependencies": (True, []),
        "_backup_dlt_state": None,
        "_get_source_total_rows": 0,
        "_get_source_table_rows": {},
        "_detect_write_disposition": full_refresh,
        "_check_oauth_token_expiry": None,
        "_inject_oauth_credentials": {},
        "_get_dataset_name": "raw_x",
        "_build_source_config": {},
        "_load_dlt_source": MagicMock(),
    }
    with (
        patch("dlt.pipeline", return_value=pipeline),
        patch("dango.ingestion.dlt_runner.os.chdir"),
        patch("dango.ingestion.dlt_runner.time.sleep"),
        patch("dango.ingestion.dlt_runner.get_source_metadata", return_value=meta),
        patch("importlib.import_module", return_value=module),
        patch.object(_db_health, "check_disk_space"),
        patch.object(
            _db_health,
            "check_duckdb_health",
            **({"side_effect": health} if health else {"return_value": {"status": "ok"}}),
        ),
        patch.object(DltPipelineRunner, "_clear_local_pipeline_cache"),
        patch.object(DltPipelineRunner, "_restore_dlt_state"),
        patch.object(DltPipelineRunner, "_cleanup_state_backup"),
        patch("dango.ingestion.dlt_runner._connect_with_lock_retry"),
        patch.multiple(
            DltPipelineRunner, **{k: MagicMock(return_value=v) for k, v in runner_patches.items()}
        ),
    ):
        return run_sync(
            project, [_source(n, kind) for n in names], skip_dbt=True, full_refresh=full_refresh
        )


@pytest.mark.parametrize("kind", ["native", "dlt"])
@pytest.mark.parametrize("label", sorted(ERRORS))
def test_failure_output_has_no_secret_and_run_completes(
    project: Path, out: StringIO, caplog: pytest.LogCaptureFixture, kind: str, label: str
) -> None:
    caplog.set_level(logging.DEBUG)
    summary = _run(project, kind, ERRORS[label], ("src_a", "src_b"))
    text = out.getvalue() + caplog.text
    for secret in SECRETS:
        assert secret not in text, f"{secret} leaked ({kind}/{label})"
    # both sources ran, the summary printed and the data shape is unchanged
    assert summary["failed_count"] == 2
    assert set(summary["failed_sources"][0]) == {"name", "error", "error_type"}
    assert "Sync Summary" in out.getvalue()
    assert "src_a" in out.getvalue() and "src_b" in out.getvalue()
    assert "Overall: 0/2" in out.getvalue()


@pytest.mark.parametrize("kind", ["native", "dlt"])
def test_markup_like_text_is_printed_literally(project: Path, out: StringIO, kind: str) -> None:
    _run(project, kind, ERRORS["markup_and_toml"], ("src_a",))
    printed = out.getvalue()
    assert "boom [/x] [red]oops[/red] [foo]" in printed


@pytest.mark.parametrize("kind", ["native", "dlt"])
def test_every_retry_attempt_is_redacted(project: Path, out: StringIO, kind: str) -> None:
    _run(project, kind, ERRORS["requests_url"], ("src_a",))
    printed = out.getvalue()
    assert printed.count("Attempt ") == 3
    assert "SECRET123" not in printed
    assert "api.example.com/v1/x" in printed


@pytest.mark.parametrize("kind", ["native", "dlt"])
def test_ordinary_error_is_printed_unchanged(project: Path, out: StringIO, kind: str) -> None:
    msg = "Server error 503 from https://api.example.com/v1/items?page=2 (host unavailable)"
    _run(project, kind, RuntimeError(msg), ("src_a",))
    assert msg in out.getvalue().replace("\n", " ")


def test_summary_prints_stored_failure_text_redacted_and_escaped(
    project: Path, out: StringIO
) -> None:
    failed = {"status": "failed", "source": "s", "error": f"x [/y] {URL_ERROR} {TOML_SECRET}"}
    with patch.object(DltPipelineRunner, "run_source", return_value=failed):
        run_sync(project, [_source("s", "dlt")], skip_dbt=True)
    printed = out.getvalue()
    assert "SECRET123" not in printed and TOML_SECRET not in printed
    assert "x [/y]" in printed


def test_full_refresh_staging_failure_is_redacted(project: Path, out: StringIO) -> None:
    runner = DltPipelineRunner(project)
    pipeline = _failing_pipeline(ERRORS["markup_and_toml"])
    with (
        patch("dlt.pipeline", return_value=pipeline),
        patch("dango.ingestion.dlt_runner.time.sleep"),
        patch("dango.ingestion.dlt_runner._connect_with_lock_retry"),
        patch.object(DltPipelineRunner, "_clear_local_pipeline_cache"),
    ):
        runner._full_refresh_via_staging(
            source=MagicMock(),
            source_name="s",
            dataset_name="raw_s",
            pipeline_name="s",
            max_lock_wait=1,
            uses_replace_mode=False,
        )
    printed = out.getvalue()
    assert TOML_SECRET not in printed
    assert "[/x]" in printed


def test_row_count_failure_is_escaped(project: Path, out: StringIO) -> None:
    runner = DltPipelineRunner(project)
    job = MagicMock()
    job.table_name = "orders"
    load_info = MagicMock()
    load_info.metrics = {"1": [{"job_metrics": {"j": job}}]}
    load_info.dataset_name = "raw_x"
    conn = MagicMock()
    conn.execute.side_effect = RuntimeError(f"count [/x] failed {TOML_SECRET}")
    with patch("dango.ingestion.dlt_runner._connect_with_lock_retry", return_value=conn):
        runner._extract_load_stats(load_info)
    printed = out.getvalue()
    assert "count [/x] failed" in printed and TOML_SECRET not in printed


@pytest.mark.parametrize("kind", ["native", "dlt"])
def test_activity_log_has_no_secret_at_rest(project: Path, out: StringIO, kind: str) -> None:
    _run(project, kind, ERRORS["requests_url"], ("src_a",))
    log = (project / ".dango" / "logs" / "activity.jsonl").read_text(encoding="utf-8")
    assert "Sync failed" in log
    assert "SECRET123" not in log


def test_log_activity_redacts_known_values(project: Path) -> None:
    from dango.utils.activity_log import log_activity

    log_activity(project, "error", "s", f"Sync failed: {TOML_SECRET} {URL_ERROR}")
    log = (project / ".dango" / "logs" / "activity.jsonl").read_text(encoding="utf-8")
    assert TOML_SECRET not in log and "SECRET123" not in log
    assert "Sync failed" in log and "api.example.com" in log


def test_db_health_failure_is_escaped(project: Path, out: StringIO) -> None:
    _run(project, "dlt", ERRORS["requests_url"], ("s",), health=RuntimeError("health [/x] bad"))
    assert "health [/x] bad" in out.getvalue()


@pytest.mark.parametrize("kind", ["native", "dlt"])
def test_drop_pipeline_state_failure_is_escaped(project: Path, out: StringIO, kind: str) -> None:
    _run(
        project,
        kind,
        ERRORS["requests_url"],
        ("s",),
        pipeline_extra={"drop": RuntimeError(f"drop [/x] bad {TOML_SECRET}")},
        full_refresh=True,
    )
    printed = out.getvalue()
    assert "Could not drop pipeline state" in printed
    assert "drop [/x] bad" in printed and TOML_SECRET not in printed


def test_inject_credentials_failure_is_escaped(project: Path, out: StringIO) -> None:
    runner = DltPipelineRunner(project)
    with patch("toml.load", side_effect=RuntimeError("inject [/x] bad")):
        runner._inject_oauth_credentials("hubspot", {})
    assert "inject [/x] bad" in out.getvalue()
