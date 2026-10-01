"""tests/unit/test_mcp_run_transform.py

MCP run_transform parity with `dango run`: structured dbt results, post-build steps,
ANSI stripping, stale run_results handling, cloud Metabase stop/start.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dango.cli.commands import mcp_operations


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mcp_operations, "_get_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "dango.visualization.metabase.refresh_metabase_connection",
        lambda project_root: (False, "not running", None),
    )
    return tmp_path


@pytest.mark.unit
class TestRunTransformParity:
    def test_run_transform_returns_structured_results_and_post_build(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A test-only failure (run_dbt_models says success) must still be reported
        as failed, with the failing test named in results.failed."""
        import json

        payload = json.dumps(
            {
                "elapsed_time": 1.5,
                "results": [
                    {
                        "unique_id": "model.p.fct_a",
                        "status": "success",
                        "message": "OK",
                        "execution_time": 0.1,
                        "failures": None,
                    },
                    {
                        "unique_id": "test.p.accepted_values_fct_a_x__a__b.abc123",
                        "status": "fail",
                        "message": "Got 1 result, configured to fail if != 0",
                        "execution_time": 0.1,
                        "failures": 1,
                    },
                ],
            }
        )

        def _fake_dbt(project_root, select, full_refresh):
            # dbt writes run_results.json during the build (after build_started)
            target = project / "dbt" / "target"
            target.mkdir(parents=True, exist_ok=True)
            (target / "run_results.json").write_text(payload)
            return (True, "Completed with 1 error (test)")

        calls: list[bool] = []
        monkeypatch.setattr(
            "dango.transformation.build_finalize.finalize_dbt_build",
            lambda project_root, *, success, refresh_metabase=True: (
                calls.append(success) or {"model_status": "updated"}
            ),
        )
        monkeypatch.setattr("dango.transformation.run_dbt_models", _fake_dbt)
        result = mcp_operations.run_transform()
        assert result["status"] == "failed"
        assert calls == [False]
        assert result["post_build"] == {"model_status": "updated"}
        assert result["results"]["counts"]["fail"] == 1
        assert [n["name"] for n in result["results"]["failed"]] == ["accepted_values_fct_a_x__a__b"]
        assert "Got 1 result" in result["results"]["failed"][0]["message"]

    def test_run_transform_stale_run_results_ignored_with_note(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A compile error leaves the previous build's run_results.json behind; it must
        not be reported as this build's results."""
        import json
        import os
        import time

        target = project / "dbt" / "target"
        target.mkdir(parents=True, exist_ok=True)
        rr = target / "run_results.json"
        rr.write_text(json.dumps({"results": [{"unique_id": "model.p.a", "status": "success"}]}))
        old = time.time() - 3600
        os.utime(rr, (old, old))
        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (False, "Compilation Error"),
        )
        result = mcp_operations.run_transform()
        assert result["status"] == "failed"
        assert result["results"] is None
        assert "no run results" in result["results_note"]

    def test_run_transform_strips_ansi(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (
                True,
                "\x1b[32mOK\x1b[0m" + "x" * 30000,
            ),
        )
        out = mcp_operations.run_transform()["output"]
        assert "\x1b" not in out
        assert len(out) == 20000

    def test_run_transform_stops_and_restarts_metabase_on_cloud(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Metabase is restarted even when dbt raises, and the lock is released once."""
        import dango.utils as dango_utils

        events: list[str] = []
        monkeypatch.setattr(
            "dango.platform.common.metabase_lifecycle.stop_metabase_for_writes",
            lambda project_root, force=False: events.append("stop") or True,
        )
        monkeypatch.setattr(
            "dango.platform.common.metabase_lifecycle.start_metabase_after_writes",
            lambda project_root: events.append("start") or True,
        )

        class FakeLock:
            def __init__(self, **kw: Any) -> None:
                self._acquired = False

            def acquire(self, timeout: float = 300) -> bool:
                self._acquired = True
                return True

            def release(self) -> None:
                events.append("release")
                self._acquired = False

        monkeypatch.setattr(dango_utils, "DbtLock", FakeLock)

        def _boom(project_root, select, full_refresh):
            events.append("dbt")
            raise RuntimeError("dbt exploded")

        monkeypatch.setattr("dango.transformation.run_dbt_models", _boom)
        result = mcp_operations.run_transform()
        assert result == {"status": "failed", "error": "dbt exploded"}
        assert events == ["stop", "dbt", "start", "release"]

    def test_run_transform_finalize_prints_never_reach_stdout(
        self, project: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """schema_manager/metabase helpers print to stdout; stdout is the JSON-RPC channel."""
        from dango.cli import schema_manager

        monkeypatch.setattr(
            "dango.transformation.run_dbt_models",
            lambda project_root, select, full_refresh: (True, "OK"),
        )
        monkeypatch.setattr(
            "dango.transformation.build_finalize.finalize_dbt_build",
            lambda project_root, *, success, refresh_metabase=True: (
                schema_manager.console.print("Created schema.yml for x") or {}
            ),
        )
        mcp_operations.run_transform()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Created schema.yml" in captured.err
