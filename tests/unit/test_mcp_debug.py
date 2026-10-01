"""tests/unit/test_mcp_debug.py

Tests for the read-only MCP debugging tools (dango/cli/commands/mcp_debug.py).
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path

import pytest

from dango.cli.commands import mcp_debug


@pytest.fixture
def project(tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch) -> Path:
    from dango.config.helpers import save_config

    save_config(sample_config, tmp_path)
    monkeypatch.setattr(mcp_debug, "_get_project_root", lambda: tmp_path)
    return tmp_path


def _write_activity(project: Path, entries: list[dict]) -> None:
    logs = project / ".dango" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "activity.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


@pytest.mark.unit
class TestValidateProject:
    def test_validate_project_returns_structured_failures(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = mcp_debug.validate_project()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert result["is_valid"] is False
        assert result["counts"]["fail"] >= 1
        assert result["checks"]
        assert all(c["status"] != "pass" for c in result["checks"])
        assert any(c["status"] == "fail" for c in result["checks"])

    def test_validate_project_skips_connectivity_by_default(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.cli.validate import ProjectValidator

        calls: list[int] = []
        monkeypatch.setattr(
            ProjectValidator, "_check_source_connectivity", lambda self: calls.append(1)
        )
        mcp_debug.validate_project()
        assert calls == []
        mcp_debug.validate_project(check_connectivity=True)
        assert calls == [1]

    def test_validate_all_display_false_prints_nothing(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from dango.cli.validate import ProjectValidator

        ProjectValidator(project).validate_all(display=False, include_connectivity=False)
        assert capsys.readouterr().out == ""


@pytest.mark.unit
class TestGetLogs:
    def test_get_logs_activity_filters_and_tail(self, project: Path) -> None:
        entries = [
            {
                "timestamp": str(i),
                "level": "error" if i % 2 == 0 else "info",
                "source": "a" if i < 8 else "b",
                "message": f"m{i}",
            }
            for i in range(10)
        ]
        _write_activity(project, entries)
        result = mcp_debug.get_logs(lines=3, level="ERROR")
        assert [e["message"] for e in result["entries"]] == ["m4", "m6", "m8"]
        result = mcp_debug.get_logs(source="b")
        assert [e["message"] for e in result["entries"]] == ["m8", "m9"]
        result = mcp_debug.get_logs(contains="M3")
        assert [e["message"] for e in result["entries"]] == ["m3"]

    def test_get_logs_source_matches_list_membership(self, project: Path) -> None:
        logs = project / ".dango" / "logs"
        logs.mkdir(parents=True)
        (logs / "dango.log").write_text(
            json.dumps({"event": "x", "level": "info", "sources": ["orders", "users"]})
            + "\n"
            + json.dumps({"event": "y", "level": "info", "sources": ["users"]})
            + "\n"
        )
        result = mcp_debug.get_logs(log="dango", source="orders")
        assert [e["event"] for e in result["entries"]] == ["x"]

    def test_get_logs_redacts_secret_keys_and_inline(self, project: Path) -> None:
        _write_activity(
            project,
            [
                {
                    "level": "info",
                    "api_key": "abcSECRET",
                    "message": "token=xyzSECRET ok",
                    "nested": {"password": "pwSECRET"},
                }
            ],
        )
        dumped = json.dumps(mcp_debug.get_logs())
        for raw in ("abcSECRET", "xyzSECRET", "pwSECRET"):
            assert raw not in dumped
        assert "****" in dumped

    def test_get_logs_non_json_lines_kept_raw(self, project: Path) -> None:
        logs = project / ".dango" / "logs"
        logs.mkdir(parents=True)
        (logs / "activity.jsonl").write_text("not json password=hunter2\n")
        result = mcp_debug.get_logs()
        assert result["entries"] == [{"raw": "not json password=****"}]

    def test_get_logs_dbt_strips_ansi_and_level_filter(self, project: Path) -> None:
        d = project / "dbt" / "logs"
        d.mkdir(parents=True)
        (d / "dbt.log").write_text(
            "12:00 [info ] \x1b[32mOK\x1b[0m model a\n"
            "12:01 [error] failed model b token=abc123\n"
            "12:02 [warn ] slow model c\n"
        )
        result = mcp_debug.get_logs(log="dbt", level="error")
        assert result["entries"] == [{"line": "12:01 [error] failed model b token=****"}]
        result = mcp_debug.get_logs(log="dbt", level="info")
        assert result["entries"] == [{"line": "12:00 [info ] OK model a"}]

    def test_get_logs_large_file_reads_tail_only(self, project: Path) -> None:
        logs = project / ".dango" / "logs"
        logs.mkdir(parents=True)
        with open(logs / "activity.jsonl", "w") as f:
            for i in range(40000):
                f.write(
                    json.dumps({"level": "info", "message": f"line-{i:06d}-" + "x" * 50}) + "\n"
                )
        assert (logs / "activity.jsonl").stat().st_size > 3 * 1024 * 1024
        result = mcp_debug.get_logs(lines=500)
        assert len(result["entries"]) == 500
        assert result["entries"][-1]["message"].startswith("line-039999")
        assert all("message" in e for e in result["entries"])  # no partial-line {"raw": ...}

    def test_get_logs_missing_file_and_unknown_log(self, project: Path) -> None:
        result = mcp_debug.get_logs()
        assert result["entries"] == [] and "not found" in result["note"]
        result = mcp_debug.get_logs(log="nope")
        assert "activity" in result["error"] and "dbt" in result["error"]

    def test_get_logs_lines_clamped(self, project: Path) -> None:
        _write_activity(project, [{"level": "info", "message": str(i)} for i in range(700)])
        assert len(mcp_debug.get_logs(lines=10000)["entries"]) == 500
        assert len(mcp_debug.get_logs(lines=0)["entries"]) == 1

    def test_get_logs_truncates_long_strings(self, project: Path) -> None:
        _write_activity(project, [{"level": "info", "message": "y" * 5000}])
        result = mcp_debug.get_logs()
        assert result["truncated"] is True
        assert len(result["entries"][0]["message"]) <= 2003


@pytest.mark.unit
class TestPlatformStatus:
    def test_platform_status_not_running(self, project: Path) -> None:
        result = mcp_debug.get_platform_status()
        assert result["server_running"] is False
        assert result["web_url"] is None
        assert result["scheduler"] is None
        assert result["warehouse"]["exists"] is False
        assert result["dbt_lock"]["held"] is False

    def test_platform_status_running_uses_project_scoped_check(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "dango.cli.helpers.process_manager.is_project_server_running", lambda root: True
        )
        result = mcp_debug.get_platform_status()
        assert result["server_running"] is True
        assert result["web_url"] == f"http://localhost:{result['port']}"

    def test_platform_status_includes_scheduler_when_running(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "dango.cli.helpers.process_manager.is_project_server_running", lambda root: True
        )
        api = {
            "running": True,
            "job_count": 2,
            "project_root": str(project),
            "schedules": [
                {"name": "a", "enabled": True, "loaded": True, "next_run_time": "x"},
                {"name": "b", "enabled": False, "loaded": False, "next_run_time": None},
            ],
        }
        monkeypatch.setattr("dango.cli.commands.schedule._query_scheduler_api", lambda root: api)
        result = mcp_debug.get_platform_status()
        assert result["scheduler"] == {"running": True, "job_count": 2, "schedules_loaded": 1}

    def test_platform_status_scheduler_none_when_not_running(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(root: Path) -> None:
            raise AssertionError("must not query when server is not running")

        monkeypatch.setattr("dango.cli.commands.schedule._query_scheduler_api", _boom)
        assert mcp_debug.get_platform_status()["scheduler"] is None


@pytest.mark.unit
@pytest.mark.skipif(sys.platform == "win32", reason="flock probe is POSIX-only")
class TestLockStatus:
    def test_lock_status_detects_held_lock(self, tmp_path: Path) -> None:
        state = tmp_path / ".dango" / "state"
        state.mkdir(parents=True)
        lock = state / "dbt.lock"
        lock.write_text("")
        (state / "dbt.lock.json").write_text(json.dumps({"operation": "run_transform"}))
        ino = os.stat(lock).st_ino
        with open(lock, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            held = mcp_debug._lock_status(tmp_path)
        assert held["held"] is True
        assert held["holder"] == {"operation": "run_transform"}
        free = mcp_debug._lock_status(tmp_path)
        assert free["held"] is False
        assert free["holder"] is None
        assert lock.exists() and os.stat(lock).st_ino == ino

    def test_lock_status_info_file_alone_is_not_held(self, tmp_path: Path) -> None:
        state = tmp_path / ".dango" / "state"
        state.mkdir(parents=True)
        (state / "dbt.lock").write_text("")
        (state / "dbt.lock.json").write_text(json.dumps({"operation": "old"}))
        result = mcp_debug._lock_status(tmp_path)
        assert result["held"] is False
        assert result["last_holder"] == {"operation": "old"}

    def test_lock_status_never_creates_lock_file(self, tmp_path: Path) -> None:
        (tmp_path / ".dango" / "state").mkdir(parents=True)
        mcp_debug._lock_status(tmp_path)
        assert not (tmp_path / ".dango" / "state" / "dbt.lock").exists()


@pytest.mark.unit
class TestWarehouseHealth:
    def test_warehouse_health_orphans(self, project: Path) -> None:
        import duckdb

        (project / "data").mkdir(exist_ok=True)
        conn = duckdb.connect(str(project / "data" / "warehouse.duckdb"))
        conn.execute("CREATE SCHEMA raw_ghost")
        conn.execute("CREATE TABLE raw_ghost.t AS SELECT 1 AS a")
        conn.close()
        result = mcp_debug.get_warehouse_health()
        assert {"schema": "raw_ghost", "table": "t", "rows": "1 rows"} in result["orphaned_tables"]
        assert result["health"]["raw_tables"] >= 1

    def test_warehouse_health_missing_warehouse(self, project: Path) -> None:
        assert mcp_debug.get_warehouse_health() == {
            "error": "No warehouse found. Run a sync first."
        }

    def test_warehouse_health_lock_conflict_is_busy_error(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (project / "data").mkdir(exist_ok=True)
        (project / "data" / "warehouse.duckdb").write_text("")

        def boom(*a, **k):
            raise RuntimeError("Could not set lock on file")

        monkeypatch.setattr("dango.utils.db_health.find_orphaned_tables", boom)
        assert "busy" in mcp_debug.get_warehouse_health()["error"]


@pytest.mark.unit
def test_debug_tools_registered() -> None:
    from dango.cli.commands.mcp_server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"validate_project", "get_logs", "get_platform_status", "get_warehouse_health"} <= names


@pytest.mark.unit
class TestRedactionAndCaps:
    @pytest.mark.parametrize(
        "text,secret",
        [
            ("client_secret=abcSEC", "abcSEC"),
            ("access_token: abcSEC", "abcSEC"),
            ("AWS_SECRET_ACCESS_KEY=abcSEC", "abcSEC"),
            ("Authorization: Bearer abcSEC", "abcSEC"),
            ('{"password": "abcSEC"}', "abcSEC"),
            ("postgres://user:abcSEC@host/db", "abcSEC"),
        ],
    )
    def test_redact_text_forms(self, text: str, secret: str) -> None:
        assert secret not in mcp_debug._redact_obj(text)

    def test_contains_filter_cannot_probe_redacted_secret(self, project: Path) -> None:
        _write_activity(project, [{"level": "info", "message": "password=hunter2"}])
        assert mcp_debug.get_logs(contains="hunter2")["entries"] == []

    def test_total_size_cap_drops_oldest(self, project: Path) -> None:
        _write_activity(
            project, [{"level": "info", "message": f"{i}" + "z" * 1900} for i in range(400)]
        )
        result = mcp_debug.get_logs(lines=500)
        assert result["truncated"] is True
        assert len(json.dumps(result["entries"])) <= mcp_debug._MAX_TOTAL
        assert result["entries"][-1]["message"].startswith("399")

    def test_tail_keeps_unicode_separator_lines_whole(self, project: Path) -> None:
        logs = project / ".dango" / "logs"
        logs.mkdir(parents=True)
        line = json.dumps({"level": "info", "message": "a b\x85c"}, ensure_ascii=False)
        (logs / "activity.jsonl").write_text(line + "\n", encoding="utf-8")
        assert mcp_debug.get_logs()["entries"][0]["message"] == "a b\x85c"
