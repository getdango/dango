"""tests/unit/test_full_refresh_pending_packages.py

After a failed load leaves a normalized package in the real pipeline, a successful `--full-refresh` of
a merge/append source must not let a later plain sync load that stale package back. A refresh that
was blocked or failed keeps it (it belongs to the load the user may still want to resume).
Real dlt + DuckDB, both sync methods.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import dlt
import pytest

from dango.ingestion.dlt_runner import DltPipelineRunner
from tests.factories.empty_guard_runs import METHODS, ids, resource, snapshot, sync

pytestmark = pytest.mark.unit

STALE = [{"id": 100 + i} for i in range(20)]
FRESH = [{"id": i} for i in (1, 2, 3)]


@pytest.fixture(autouse=True)
def _isolate_dlt_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DLT_DATA_DIR", str(tmp_path / "dlt_data"))


def source(disposition: str, rows: list[dict[str, int]], *, narrowed: bool = False) -> Any:
    """Tables a (the one under test) and b; `narrowed` selects only a."""
    hints = {"primary_key": "id"} if disposition == "merge" else {}

    @dlt.source(name="fr")
    def _src() -> Any:
        yield resource("a", rows, disposition, **hints)
        yield resource("b", FRESH, disposition, **hints)

    src = _src()
    return src.with_resources("a") if narrowed else src


def failed_load(runner: DltPipelineRunner, method: str, src: Any) -> None:
    """A sync whose load step fails after extract and normalize (a package stays pending)."""
    with (
        patch.object(DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("load failed")),
        pytest.raises(RuntimeError, match="load failed"),
    ):
        sync(runner, method, "fr", src)
    assert has_pending_package()


def has_pending_package() -> bool:
    return bool(dlt.attach("fr").has_pending_data)


def a_ids(runner: DltPipelineRunner) -> list[int]:
    return ids(runner, "fr", "a")


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("disposition", ["merge", "append"])
@pytest.mark.parametrize("narrowed", [False, True], ids=["all-selected", "narrowed"])
class TestFullRefreshClearsTheStalePackage:
    def test_a_later_plain_sync_does_not_bring_the_failed_load_back(
        self, tmp_path: Any, method: str, disposition: str, narrowed: bool
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "fr", source(disposition, FRESH))["status"] == "success"
        failed_load(runner, method, source(disposition, STALE, narrowed=narrowed))
        before_b = snapshot(runner, "fr")["b"]

        refreshed = sync(
            runner,
            method,
            "fr",
            source(disposition, FRESH, narrowed=narrowed),
            full_refresh=True,
        )
        assert refreshed["status"] == "success", refreshed.get("error")
        assert a_ids(runner) == [1, 2, 3]
        pending_after_refresh = has_pending_package()

        plain = sync(runner, method, "fr", source(disposition, FRESH, narrowed=narrowed))
        assert plain["status"] == "success", plain.get("error")
        expected = [1, 2, 3] if disposition == "merge" else [1, 1, 2, 2, 3, 3]
        assert a_ids(runner) == expected  # not the 20 stale rows as well
        assert not pending_after_refresh
        if narrowed:
            assert snapshot(runner, "fr")["b"] == before_b  # b was never part of these runs


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("disposition", ["merge", "append"])
class TestKeptWhenTheRefreshDidNotHappen:
    def test_a_refresh_blocked_by_the_empty_guard_keeps_the_pending_package(
        self, tmp_path: Any, method: str, disposition: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "fr", source(disposition, FRESH))["status"] == "success"
        failed_load(runner, method, source(disposition, STALE))
        blocked = sync(runner, method, "fr", source(disposition, []), full_refresh=True)
        assert blocked["status"] == "failed"
        assert has_pending_package()
        assert a_ids(runner) == [1, 2, 3]  # the destination was never touched

    def test_a_refresh_that_failed_in_staging_keeps_the_pending_package(
        self, tmp_path: Any, method: str, disposition: str
    ) -> None:
        runner = DltPipelineRunner(tmp_path)
        assert sync(runner, method, "fr", source(disposition, FRESH))["status"] == "success"
        failed_load(runner, method, source(disposition, STALE))
        with patch.object(
            DltPipelineRunner, "_load_with_lock", side_effect=RuntimeError("staging load failed")
        ):
            failed = sync(runner, method, "fr", source(disposition, FRESH), full_refresh=True)
        assert failed["status"] == "failed"
        assert has_pending_package()
        assert a_ids(runner) == [1, 2, 3]
