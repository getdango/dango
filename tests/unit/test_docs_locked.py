"""tests/unit/test_docs_locked.py

`generate_dbt_docs_locked`: dbt docs generation under the dbt lock that never fails its caller, and
the callers that run it outside any other lock holder.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from dango.exceptions import DbtLockError
from dango.utils.dbt_lock import DbtLock

pytestmark = pytest.mark.unit


def _lock_is_free(root: Path) -> bool:
    probe = DbtLock(root, source="probe", operation="probe")
    try:
        probe.acquire(timeout=0)
    except DbtLockError:
        return False
    probe.release()
    return True


def test_docs_generated_with_the_lock_held_and_released_after(tmp_path: Path) -> None:
    from dango.transformation.docs_locked import generate_dbt_docs_locked

    held: list[bool] = []

    def docs(root: Path) -> tuple[bool, str]:
        held.append(not _lock_is_free(root))
        return (True, "catalog written")

    with patch("dango.transformation.generate_dbt_docs", side_effect=docs):
        assert generate_dbt_docs_locked(tmp_path) == (True, "catalog written")

    assert held == [True]
    assert _lock_is_free(tmp_path)


def test_busy_lock_warns_once_and_is_not_run_without_raising(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from dango.transformation.docs_locked import generate_dbt_docs_locked

    other = DbtLock(tmp_path, source="sync", operation="load:x")
    other.acquire(timeout=0)
    try:
        with (
            patch("dango.transformation.generate_dbt_docs") as docs,
            caplog.at_level("WARNING"),
        ):
            ok, message = generate_dbt_docs_locked(tmp_path, timeout=0)
    finally:
        other.release()

    docs.assert_not_called()
    assert ok is False
    assert "busy" in message.lower()
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_docs_failure_is_returned_not_raised_and_releases_the_lock(tmp_path: Path) -> None:
    from dango.transformation.docs_locked import generate_dbt_docs_locked

    with patch("dango.transformation.generate_dbt_docs", side_effect=OSError("disk gone")):
        ok, message = generate_dbt_docs_locked(tmp_path)

    assert ok is False
    assert "disk gone" in message
    assert _lock_is_free(tmp_path)


def test_a_failing_docs_run_is_returned_unchanged(tmp_path: Path) -> None:
    from dango.transformation.docs_locked import generate_dbt_docs_locked

    with patch("dango.transformation.generate_dbt_docs", return_value=(False, "dbt said no")):
        assert generate_dbt_docs_locked(tmp_path) == (False, "dbt said no")


def _holds_lock_probe(root: Path, seen: list[bool]) -> object:
    def docs(*_a: object, **_k: object) -> tuple[bool, str]:
        seen.append(not _lock_is_free(root))
        return (True, "")

    return docs


def test_initial_sync_docs_run_under_the_lock(tmp_path: Path) -> None:
    from dango.web.routes.initial_sync import _generate_dbt_docs

    seen: list[bool] = []
    with patch(
        "dango.transformation.generate_dbt_docs", side_effect=_holds_lock_probe(tmp_path, seen)
    ):
        _generate_dbt_docs(tmp_path)
    assert seen == [True]


def test_watcher_docs_regeneration_runs_under_the_lock(tmp_path: Path) -> None:
    from dango.platform.local.watcher_runner import regenerate_dbt_docs

    seen: list[bool] = []
    with patch(
        "dango.transformation.generate_dbt_docs", side_effect=_holds_lock_probe(tmp_path, seen)
    ):
        regenerate_dbt_docs(tmp_path)
    assert seen == [True]


def test_watcher_docs_regeneration_survives_a_busy_warehouse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dango.platform.local.watcher_runner import regenerate_dbt_docs

    other = DbtLock(tmp_path, source="sync", operation="load:x")
    other.acquire(timeout=0)
    try:
        with patch("dango.transformation.docs_locked.DOCS_LOCK_WAIT_SECONDS", 0):
            regenerate_dbt_docs(tmp_path)
    finally:
        other.release()

    assert "dbt docs generation failed" in capsys.readouterr().out


def test_source_remove_docs_run_under_the_lock(
    tmp_path: Path, sample_config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from dango.cli.main import cli
    from tests.unit.test_wp20r_followups import _project_with_source

    project = _project_with_source(tmp_path, sample_config)
    monkeypatch.chdir(project)
    seen: list[bool] = []
    with patch(
        "dango.transformation.generate_dbt_docs", side_effect=_holds_lock_probe(project, seen)
    ):
        result = CliRunner().invoke(cli, ["source", "remove", "orders", "--yes"])

    assert result.exit_code == 0, result.output
    assert seen == [True]


def test_source_remove_does_not_claim_regenerated_docs_when_the_warehouse_is_busy(
    tmp_path: Path, sample_config: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from click.testing import CliRunner

    from dango.cli.main import cli
    from tests.unit.test_wp20r_followups import _project_with_source

    project = _project_with_source(tmp_path, sample_config)
    monkeypatch.chdir(project)
    other = DbtLock(project, source="sync", operation="load:x")
    other.acquire(timeout=0)
    try:
        with patch("dango.transformation.docs_locked.DOCS_LOCK_WAIT_SECONDS", 0):
            result = CliRunner().invoke(cli, ["source", "remove", "orders", "--yes"])
    finally:
        other.release()

    assert result.exit_code == 0, result.output
    assert "dbt documentation regenerated" not in result.output
    assert "Could not regenerate dbt documentation" in " ".join(result.output.split())


def test_sync_docs_run_under_the_lock_after_the_dbt_lock_is_released(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from dango.ingestion.dlt_runner import run_sync

    source = MagicMock()
    source.name = "test_source"
    source.type.value = "hubspot"
    source.enabled = True
    source.csv = None
    source.dlt_native = None
    runner = MagicMock()
    runner.run_source.return_value = {"status": "success", "source": "x", "rows_loaded": 1}
    generator = MagicMock()
    generator.generate_all_models.return_value = {"generated": []}
    seen: list[bool] = []

    with (
        patch("dango.ingestion.dlt_runner.DltPipelineRunner", return_value=runner),
        patch("dango.transformation.generator.DbtModelGenerator", return_value=generator),
        patch("dango.governance.schema_drift.detect_drift_for_sources", return_value=[]),
        patch("dango.transformation.run_dbt_models", return_value=(True, "OK")),
        patch(
            "dango.transformation.generate_dbt_docs",
            side_effect=_holds_lock_probe(tmp_path, seen),
        ),
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(False, None, None),
        ),
        patch("dango.visualization.metabase.sync_metabase_schema", return_value=False),
        patch("dango.utils.post_sync.dispatch_post_sync_hooks", return_value=None),
        patch("duckdb.connect") as connect,
        patch("dango.ingestion.dlt_runner.console"),
    ):
        connect.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = []
        run_sync(project_root=tmp_path, sources=[source], max_lock_wait=5)

    assert seen == [True]


@pytest.mark.anyio
async def test_initial_sync_docs_do_not_run_on_the_event_loop_thread(tmp_path: Path) -> None:
    import threading
    from unittest.mock import AsyncMock

    import dango.web.routes.initial_sync as mod

    loop_thread = threading.get_ident()
    docs_threads: list[int] = []
    mod._sync_state = mod.InitialSyncState()

    with (
        patch.object(mod, "_save_and_broadcast", new_callable=AsyncMock),
        patch.object(mod, "_sync_single_source", new_callable=AsyncMock),
        patch.object(mod, "_check_disk_usage", return_value=None),
        patch.object(mod, "_refresh_metabase"),
        patch.object(
            mod,
            "_generate_dbt_docs",
            side_effect=lambda *_a: docs_threads.append(threading.get_ident()),
        ),
    ):
        await mod._run_initial_sync(tmp_path, [{"name": "hubspot", "type": "hubspot"}])

    assert len(docs_threads) == 1
    assert docs_threads[0] != loop_thread
