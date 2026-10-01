"""tests/unit/test_mcp_operations.py

Tests for the MCP operate tools (dango/cli/commands/mcp_operations.py):
run_sync (CLI parity), run_transform, run_doctor.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from dango.cli.commands import mcp_operations


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real Dango project on disk (enabled CSV source 'test_source', enabled
    local_files source 'orders', disabled local_files source 'archive'), with
    mcp_operations._get_project_root() pointed at it and the Metabase refresh
    functions stubbed (not running)."""
    from dango.config.helpers import save_config
    from dango.config.models import DataSource, LocalFilesSourceConfig, SourceType

    for name, enabled in (("orders", True), ("archive", False)):
        sample_config.sources.sources.append(
            DataSource(
                name=name,
                type=SourceType.LOCAL_FILES,
                enabled=enabled,
                local_files=LocalFilesSourceConfig(directory=f"data/{name}"),
            )
        )
    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_operations, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "dango.visualization.metabase.refresh_metabase_connection",
        lambda project_root: (False, "not running", None),
    )
    monkeypatch.setattr(
        "dango.visualization.metabase.sync_metabase_schema",
        lambda project_root, existing_session_id=None: False,
    )
    return tmp_path


class FakeSync:
    """Records run_sync calls and returns a configurable summary."""

    def __init__(self, summary: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.summary = {"success_count": 1, "failed_count": 0} if summary is None else summary

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.summary


@pytest.fixture
def fake_sync(monkeypatch: pytest.MonkeyPatch) -> FakeSync:
    fake = FakeSync()
    monkeypatch.setattr("dango.ingestion.run_sync", fake)
    return fake


@pytest.mark.unit
class TestRunSync:
    def test_run_sync_missing_source(self, project: Path) -> None:
        result = mcp_operations.run_sync("nonexistent")
        assert result["error"] == "Source 'nonexistent' not found in sources.yml"
        assert {"name": "archive", "type": "local_files", "enabled": False} in result["available"]

    def test_unknown_source_lists_available(self, project: Path, fake_sync: FakeSync) -> None:
        result = mcp_operations.run_sync("nope")
        assert [s["name"] for s in result["available"]] == ["test_source", "orders", "archive"]
        assert fake_sync.calls == []

    def test_run_sync_calls_real_function_with_source_object_list(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the source_names-kwarg bug: run_sync()'s real signature
        takes sources: list[DataSource], not a source_names kwarg. Assert the tool calls
        it with a list containing the actual DataSource object, not source name strings."""
        captured: dict[str, Any] = {}

        def fake_run_sync(*, project_root, sources, full_refresh, **kwargs):
            captured["project_root"] = project_root
            captured["sources"] = sources
            captured["full_refresh"] = full_refresh
            return {"status": "completed", "rows_loaded": 42}

        monkeypatch.setattr("dango.ingestion.run_sync", fake_run_sync)

        result = mcp_operations.run_sync("test_source", full_refresh=True)

        assert result["rows_loaded"] == 42
        assert result["status"] == "completed"
        assert captured["project_root"] == project
        assert len(captured["sources"]) == 1
        assert captured["sources"][0].name == "test_source"
        assert captured["full_refresh"] is True

    def test_run_sync_wraps_exception(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def raising_run_sync(**kwargs):
            raise RuntimeError("lock held by another process")

        monkeypatch.setattr("dango.ingestion.run_sync", raising_run_sync)

        result = mcp_operations.run_sync("test_source")
        assert result == {"status": "failed", "error": "lock held by another process"}

    def test_non_dict_return_is_completed(self, project: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr("dango.ingestion.run_sync", lambda **kw: None)
        assert mcp_operations.run_sync("test_source") == {"status": "completed"}

    def test_backfill_conflicts_with_since(self, project: Path, fake_sync: FakeSync) -> None:
        result = mcp_operations.run_sync(backfill="7d", since="2026-01-01")
        assert result == {"error": "backfill conflicts with since/until. Use one or the other."}
        assert fake_sync.calls == []

    def test_limit_must_be_positive(self, project: Path, fake_sync: FakeSync) -> None:
        assert mcp_operations.run_sync(limit=0) == {"error": "limit must be a positive integer."}
        assert fake_sync.calls == []

    def test_invalid_since_format(self, project: Path, fake_sync: FakeSync) -> None:
        result = mcp_operations.run_sync(since="01/01/2026")
        assert result == {"error": "Invalid since date format. Use YYYY-MM-DD"}
        result = mcp_operations.run_sync(until="nope")
        assert result == {"error": "Invalid until date format. Use YYYY-MM-DD"}
        assert fake_sync.calls == []

    def test_since_must_be_before_until(self, project: Path, fake_sync: FakeSync) -> None:
        result = mcp_operations.run_sync(since="2026-02-01", until="2026-01-01")
        assert result == {"error": "since must be before until."}
        assert fake_sync.calls == []

    def test_bad_backfill_returns_error(self, project: Path, fake_sync: FakeSync) -> None:
        result = mcp_operations.run_sync(backfill="seven")
        assert "Invalid duration 'seven'" in result["error"]
        assert fake_sync.calls == []

    def test_backfill_dates_match_cli_arithmetic(self, project: Path, fake_sync: FakeSync) -> None:
        mcp_operations.run_sync(backfill="7d")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        call = fake_sync.calls[0]
        assert call["end_date"] == today
        assert call["start_date"] == today - timedelta(days=7)

    def test_all_enabled_sources_when_name_omitted(
        self, project: Path, fake_sync: FakeSync
    ) -> None:
        mcp_operations.run_sync()
        assert [s.name for s in fake_sync.calls[0]["sources"]] == ["test_source", "orders"]

    def test_named_disabled_source_is_synced(self, project: Path, fake_sync: FakeSync) -> None:
        mcp_operations.run_sync("archive")
        assert [s.name for s in fake_sync.calls[0]["sources"]] == ["archive"]

    def test_named_disabled_source_warns_it_will_be_skipped(
        self, project: Path, fake_sync: FakeSync
    ) -> None:
        result = mcp_operations.run_sync("archive")
        assert any("'archive' is disabled" in w for w in result["warnings"])

    def test_oauth_precheck_unexpected_error_returns_failed(
        self, project: Path, fake_sync: FakeSync, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(source_type, project_root):
            raise OSError("corrupt secrets.toml")

        monkeypatch.setattr("dango.oauth.validation.validate_before_sync", boom)
        result = mcp_operations.run_sync("orders")
        assert result == {"status": "failed", "error": "corrupt secrets.toml", "source": "orders"}
        assert fake_sync.calls == []

    def test_dry_run_runs_nothing_and_reports_plan(
        self, project: Path, fake_sync: FakeSync
    ) -> None:
        result = mcp_operations.run_sync("orders", since="2026-01-01", limit=5, dry_run=True)
        assert fake_sync.calls == []
        assert result["status"] == "dry_run"
        assert result["sources"] == [{"name": "orders", "type": "local_files", "enabled": True}]
        assert result["options"] == {
            "full_refresh": False,
            "since": "2026-01-01",
            "until": None,
            "limit": 5,
            "allow_schema_changes": False,
            "allow_empty_replace": None,
        }
        assert any(
            "'orders' (local_files) does not support date range" in w for w in result["warnings"]
        )

    def test_oauth_precheck_blocks_sync(
        self, project: Path, fake_sync: FakeSync, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.exceptions import OAuthTokenExpiredError

        def raise_expired(source_type, project_root):
            raise OAuthTokenExpiredError("expired", user_message="Token expired")

        monkeypatch.setattr("dango.oauth.validation.validate_before_sync", raise_expired)
        result = mcp_operations.run_sync("orders")
        assert result == {
            "status": "failed",
            "error": "Token expired",
            "source": "orders",
            "action": "dango oauth local_files",
        }
        assert fake_sync.calls == []

    def test_empty_replace_and_schema_flags_forwarded(
        self, project: Path, fake_sync: FakeSync
    ) -> None:
        mcp_operations.run_sync(
            "orders", limit=10, allow_schema_changes=True, allow_empty_replace=False
        )
        call = fake_sync.calls[0]
        assert call["allow_schema_changes"] is True
        assert call["allow_empty_replace"] is False
        assert call["limit"] == 10

    def test_metabase_refresh_only_on_success(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        refresh_calls: list[Path] = []
        schema_calls: list[Any] = []

        def refresh(project_root):
            refresh_calls.append(project_root)
            return True, None, "sess"

        def schema(project_root, existing_session_id=None):
            schema_calls.append(existing_session_id)
            return True

        monkeypatch.setattr("dango.visualization.metabase.refresh_metabase_connection", refresh)
        monkeypatch.setattr("dango.visualization.metabase.sync_metabase_schema", schema)

        monkeypatch.setattr(
            "dango.ingestion.run_sync", lambda **kw: {"success_count": 1, "failed_count": 0}
        )
        result = mcp_operations.run_sync("orders")
        assert result["metabase"] == "refreshed"
        assert refresh_calls == [project] and schema_calls == ["sess"]

        refresh_calls.clear()
        monkeypatch.setattr(
            "dango.ingestion.run_sync", lambda **kw: {"success_count": 0, "failed_count": 1}
        )
        result = mcp_operations.run_sync("orders")
        assert refresh_calls == []
        assert "metabase" not in result

    def test_metabase_not_running(self, project: Path, fake_sync: FakeSync) -> None:
        assert mcp_operations.run_sync("orders")["metabase"] == "not_running"

    def test_status_field_completed_partial_failed(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for summary, expected in (
            ({"success_count": 2, "failed_count": 0}, "completed"),
            ({"success_count": 1, "failed_count": 1}, "partial"),
            ({"success_count": 0, "failed_count": 2}, "failed"),
        ):
            monkeypatch.setattr("dango.ingestion.run_sync", lambda s=summary, **kw: dict(s))
            result = mcp_operations.run_sync()
            assert result["status"] == expected
            assert result["warnings"] == []
            assert result["success_count"] == summary["success_count"]

    def test_full_refresh_warning(self, project: Path, fake_sync: FakeSync) -> None:
        result = mcp_operations.run_sync("orders", full_refresh=True)
        assert "Full refresh: existing data will be dropped and reloaded" in result["warnings"]


@pytest.mark.unit
class TestRunTransform:
    def test_run_transform_success(self, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (True, "1 of 1 OK"),
        )
        result = mcp_operations.run_transform()
        assert result == {"status": "completed", "output": "1 of 1 OK"}

    def test_run_transform_failure_is_not_reported_as_completed(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the tuple-truthiness bug: run_dbt_models() returns
        tuple[bool, str] — `if result` on the raw tuple is always truthy regardless of
        the bool inside it, so a real dbt failure must still surface as status='failed'."""
        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (False, "Compilation Error in model foo"),
        )
        result = mcp_operations.run_transform()
        assert result == {"status": "failed", "output": "Compilation Error in model foo"}

    def test_run_transform_acquires_and_releases_dbt_lock(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression test for BUGS-FOUND.md 'MCP run_transform does not acquire
        DbtLock': run_transform() was the one caller of run_dbt_models() in the
        whole codebase with no lock acquisition (unlike dlt_runner.py,
        web/routes/upload.py, and platform/scheduling/jobs.py x2). Assert the
        real call order is acquire -> dbt -> release, matching transform.py's
        `run()` CLI command."""
        import dango.utils as dango_utils

        call_order: list[str] = []

        class FakeLock:
            def __init__(self, *, project_root, source, operation):
                call_order.append(f"init:{source}")
                self._acquired = False

            def acquire(self, timeout: float = 300) -> bool:
                call_order.append("acquire")
                self._acquired = True
                return True

            def release(self) -> None:
                call_order.append("release")
                self._acquired = False

        monkeypatch.setattr(dango_utils, "DbtLock", FakeLock)

        def _track_dbt(project_root, select, full_refresh):
            call_order.append("dbt")
            return (True, "1 of 1 OK")

        monkeypatch.setattr("dango.transformation.run_dbt_models", _track_dbt)

        result = mcp_operations.run_transform()

        assert result == {"status": "completed", "output": "1 of 1 OK"}
        assert call_order == ["init:mcp", "acquire", "dbt", "release"]

    def test_run_transform_lock_timeout_returns_clean_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When DbtLock can't be acquired (e.g. a sync or scheduled job is already
        writing to DuckDB), run_transform() must return the tool's normal
        {"status": "failed", "error": ...} shape — same convention as every other
        error path in this function — rather than letting DbtLockError propagate
        unhandled through the MCP tool boundary. Also confirms run_dbt_models()
        is never reached when the lock can't be acquired."""
        from dango.exceptions import DbtLockError
        from dango.utils import DbtLock

        def _raise_lock_error(self, timeout: float = 300) -> bool:
            raise DbtLockError("Sync queue timeout. Another sync is still running.")

        monkeypatch.setattr(DbtLock, "acquire", _raise_lock_error)

        dbt_called = False

        def _track_dbt(project_root, select, full_refresh):
            nonlocal dbt_called
            dbt_called = True
            return (True, "should not run")

        monkeypatch.setattr("dango.transformation.run_dbt_models", _track_dbt)

        result = mcp_operations.run_transform()

        assert result == {
            "status": "failed",
            "error": "Sync queue timeout. Another sync is still running.",
        }
        assert dbt_called is False


@pytest.mark.unit
class TestRunDoctor:
    def test_run_doctor_delegates_to_get_cached_credential_health(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Positive control for the run_doctor_cached-doesn't-exist bug: the real
        function is get_cached_credential_health() in dango.ingestion.credential_health."""
        expected = [{"source": "test_source", "type": "csv", "status": "ok", "detail": ""}]
        monkeypatch.setattr(
            "dango.ingestion.credential_health.get_cached_credential_health",
            lambda project_root: expected,
        )
        assert mcp_operations.run_doctor() == expected
