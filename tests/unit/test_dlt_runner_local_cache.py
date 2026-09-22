"""tests/unit/test_dlt_runner_local_cache.py

Unit tests for 1.0.8-Q6: --full-refresh must clear the local dlt pipeline
cache at ~/.dlt/pipelines/{pipeline_name}/, not just the destination DB
schema.

Split out of test_dlt_runner_full_refresh.py (which covers the pre-existing
BUG-229 backup/restore behavior) to keep both files under the 500-line
file-size-check limit.

Tests cover:
- _clear_local_pipeline_cache() removes the directory when it exists, no-ops
  when it doesn't, and swallows (logs) errors rather than raising
- Both _run_dlt_native_source and _run_dlt_source call it after pipeline.drop()
  inside `if full_refresh:` — and, critically, still call it even when
  pipeline.drop() itself raises (the actual production bug)
- A non-full-refresh sync never calls it / never touches the directory
- Both methods re-create the `pipeline` object after clearing the cache —
  found via live testing on a scratch project: dlt's Pipeline instance does
  not lazily recreate its on-disk working directory before
  extract()/normalize()/load(), so reusing the pre-clear pipeline object
  raises FileNotFoundError on schemas/ mid-sync
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


def _patch_home(monkeypatch, fake_home):
    """Redirect os.path.expanduser("~...") to fake_home for the duration of a test.

    Both _backup_dlt_state and the new _clear_local_pipeline_cache resolve the
    dlt state directory via os.path.expanduser("~/.dlt"), so patching the
    global os.path.expanduser (rather than anything module-local) covers both.
    """
    monkeypatch.setattr(
        "os.path.expanduser",
        lambda p: str(fake_home) + p[1:] if p.startswith("~") else p,
    )


@pytest.mark.unit
class TestClearLocalPipelineCache:
    """Direct unit tests for the new _clear_local_pipeline_cache() helper
    (1.0.8-Q6), isolated from the surrounding sync flow."""

    def _make_runner(self):
        from dango.ingestion.dlt_runner import DltPipelineRunner

        return DltPipelineRunner.__new__(DltPipelineRunner)

    def test_removes_existing_pipeline_dir(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        pipeline_dir = fake_home / ".dlt" / "pipelines" / "my_pipeline"
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "state.json").write_text("{}")
        _patch_home(monkeypatch, fake_home)

        runner = self._make_runner()
        runner._clear_local_pipeline_cache("my_pipeline")

        assert not pipeline_dir.exists()

    def test_noop_when_dir_missing(self, tmp_path, monkeypatch):
        """Must not raise when there's nothing to clear (e.g. first-ever sync)."""
        fake_home = tmp_path / "home"
        _patch_home(monkeypatch, fake_home)

        runner = self._make_runner()
        runner._clear_local_pipeline_cache("never_synced_pipeline")  # no raise

    @patch("dango.ingestion.dlt_runner.console")
    def test_swallows_rmtree_exception(self, mock_console, tmp_path, monkeypatch):
        """If rmtree itself fails, log it (matches pipeline.drop()'s existing
        catch-and-log convention) rather than crashing the sync."""
        fake_home = tmp_path / "home"
        pipeline_dir = fake_home / ".dlt" / "pipelines" / "locked_pipeline"
        pipeline_dir.mkdir(parents=True)
        _patch_home(monkeypatch, fake_home)

        runner = self._make_runner()
        with patch("shutil.rmtree", side_effect=OSError("simulated: file in use")):
            runner._clear_local_pipeline_cache("locked_pipeline")  # no raise

        assert mock_console.print.called


@pytest.mark.unit
class TestFullRefreshClearsLocalPipelineCacheNative:
    """1.0.8-Q6 regression tests for _run_dlt_native_source.

    Real production bug: pipeline.drop() alone was relied on to clear
    ~/.dlt/pipelines/{pipeline_name}/. Its failure was caught and only logged,
    so a full refresh could silently proceed with stale local incremental
    cursor state intact even though the destination schema was dropped.
    """

    def _make_runner(self, tmp_path):
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        runner._current_oauth_warning = None
        return runner

    def _make_native_source_config(self, pipeline_name="test_pipeline"):
        config = MagicMock()
        config.name = "test_source"
        config.type.value = "dlt_native"
        config.dlt_native.source_module = "test_module"
        config.dlt_native.source_function = "test_func"
        config.dlt_native.pipeline_name = pipeline_name
        config.dlt_native.dataset_name = "raw_test"
        config.dlt_native.source_args = {}
        return config

    def _make_stale_cache_dir(self, tmp_path, monkeypatch, pipeline_name="test_pipeline"):
        fake_home = tmp_path / "home"
        pipeline_dir = fake_home / ".dlt" / "pipelines" / pipeline_name
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "state.json").write_text('{"cursor": "stale"}')
        _patch_home(monkeypatch, fake_home)
        return pipeline_dir

    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("dango.ingestion.dlt_runner.importlib")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_full_refresh_clears_local_pipeline_cache_after_pipeline_drop_succeeds(
        self,
        mock_getcwd,
        mock_chdir,
        mock_importlib,
        mock_dlt,
        mock_console,
        tmp_path,
        monkeypatch,
    ):
        pipeline_dir = self._make_stale_cache_dir(tmp_path, monkeypatch)

        runner = self._make_runner(tmp_path)
        config = self._make_native_source_config()

        mock_source = MagicMock()
        mock_module = MagicMock()
        mock_module.test_func.return_value = mock_source
        mock_importlib.import_module.return_value = mock_module

        mock_pipeline = MagicMock()  # pipeline.drop() succeeds by default
        mock_dlt.pipeline.return_value = mock_pipeline

        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 5})

        result = runner._run_dlt_native_source(config, full_refresh=True)

        assert result["status"] == "success"
        assert not pipeline_dir.exists()
        # Regression check (found via live testing on a scratch project): the
        # pipeline object must be re-created after the clear, or dlt's
        # Pipeline instance raises FileNotFoundError on schemas/ during the
        # extract phase that follows, because it was constructed against the
        # working directory we just deleted.
        assert mock_dlt.pipeline.call_count == 2

    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("dango.ingestion.dlt_runner.importlib")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_full_refresh_clears_local_pipeline_cache_even_if_pipeline_drop_fails(
        self,
        mock_getcwd,
        mock_chdir,
        mock_importlib,
        mock_dlt,
        mock_console,
        tmp_path,
        monkeypatch,
    ):
        """THE regression test for 1.0.8-Q6.

        Mocks pipeline.drop() to raise, simulating the exact observed failure
        mode. This must FAIL against the pre-fix code (the local cache would
        survive) and PASS against the fix.
        """
        pipeline_dir = self._make_stale_cache_dir(tmp_path, monkeypatch)

        runner = self._make_runner(tmp_path)
        config = self._make_native_source_config()

        mock_source = MagicMock()
        mock_module = MagicMock()
        mock_module.test_func.return_value = mock_source
        mock_importlib.import_module.return_value = mock_module

        mock_pipeline = MagicMock()
        mock_pipeline.drop.side_effect = RuntimeError("simulated pipeline.drop() failure")
        mock_dlt.pipeline.return_value = mock_pipeline

        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 5})

        result = runner._run_dlt_native_source(config, full_refresh=True)

        # Sync proceeds despite pipeline.drop() failing (existing catch-and-log
        # behavior for that call is unchanged).
        assert result["status"] == "success"
        # The actual regression assertion.
        assert not pipeline_dir.exists(), (
            "local dlt pipeline cache survived a failed pipeline.drop() call — "
            "_clear_local_pipeline_cache() did not run (or did not fail-safe)"
        )
        assert mock_dlt.pipeline.call_count == 2

    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("dango.ingestion.dlt_runner.importlib")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_non_full_refresh_sync_never_touches_local_pipeline_cache(
        self,
        mock_getcwd,
        mock_chdir,
        mock_importlib,
        mock_dlt,
        mock_console,
        tmp_path,
        monkeypatch,
    ):
        pipeline_dir = self._make_stale_cache_dir(tmp_path, monkeypatch)

        runner = self._make_runner(tmp_path)
        config = self._make_native_source_config()

        mock_source = MagicMock()
        mock_module = MagicMock()
        mock_module.test_func.return_value = mock_source
        mock_importlib.import_module.return_value = mock_module

        mock_pipeline = MagicMock()
        mock_dlt.pipeline.return_value = mock_pipeline

        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 5})
        runner._clear_local_pipeline_cache = MagicMock()

        result = runner._run_dlt_native_source(config, full_refresh=False)

        assert result["status"] == "success"
        runner._clear_local_pipeline_cache.assert_not_called()
        assert pipeline_dir.exists()
        # Non-full-refresh must not pay the pipeline-recreation cost either.
        assert mock_dlt.pipeline.call_count == 1


@pytest.mark.unit
class TestFullRefreshClearsLocalPipelineCacheDltSource:
    """Mirrors TestFullRefreshClearsLocalPipelineCacheNative for _run_dlt_source.

    Unlike _run_dlt_native_source, this method has no separate `pipeline_name`
    config override — dlt.pipeline(pipeline_name=source_name, ...) always uses
    source_name directly, so the local cache directory is keyed on source_name.
    """

    def _make_runner(self, tmp_path):
        from dango.ingestion.dlt_runner import DltPipelineRunner

        runner = DltPipelineRunner.__new__(DltPipelineRunner)
        runner.project_root = tmp_path
        runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
        runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
        runner._current_oauth_warning = None
        return runner

    def _make_source_config(self, name="test_source2"):
        config = MagicMock()
        config.name = name
        return config

    def _stub_runner_methods(self, runner):
        """Stub out the parts of _run_dlt_source unrelated to this fix
        (source-kwargs assembly, OAuth, registry-driven dataset naming, and
        the dynamic dlt source import) so the test exercises the real
        full_refresh / pipeline.drop() / _clear_local_pipeline_cache path."""
        runner._build_source_config = MagicMock(return_value={})
        runner._check_oauth_token_expiry = MagicMock(return_value=None)
        runner._inject_oauth_credentials = MagicMock(side_effect=lambda _type, kwargs: kwargs)
        runner._get_dataset_name = MagicMock(return_value="raw_test_source2")
        runner._load_dlt_source = MagicMock(return_value=MagicMock())
        runner._extract_load_stats = MagicMock(return_value={"rows_loaded": 5})

    def _make_stale_cache_dir(self, tmp_path, monkeypatch, source_name="test_source2"):
        fake_home = tmp_path / "home"
        pipeline_dir = fake_home / ".dlt" / "pipelines" / source_name
        pipeline_dir.mkdir(parents=True)
        (pipeline_dir / "state.json").write_text('{"cursor": "stale"}')
        _patch_home(monkeypatch, fake_home)
        return pipeline_dir

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_full_refresh_clears_local_pipeline_cache_after_pipeline_drop_succeeds(
        self,
        mock_getcwd,
        mock_chdir,
        mock_dlt,
        mock_console,
        mock_get_metadata,
        tmp_path,
        monkeypatch,
    ):
        pipeline_dir = self._make_stale_cache_dir(tmp_path, monkeypatch)
        mock_get_metadata.return_value = {"dlt_package": "pkg", "dlt_function": "func"}

        runner = self._make_runner(tmp_path)
        self._stub_runner_methods(runner)
        config = self._make_source_config()

        mock_pipeline = MagicMock()  # pipeline.drop() succeeds by default
        mock_dlt.pipeline.return_value = mock_pipeline

        result = runner._run_dlt_source(config, full_refresh=True)

        assert result["status"] == "success"
        assert not pipeline_dir.exists()
        # Regression check (found via live testing on a scratch project): see
        # the identical comment in TestFullRefreshClearsLocalPipelineCacheNative.
        assert mock_dlt.pipeline.call_count == 2

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_full_refresh_clears_local_pipeline_cache_even_if_pipeline_drop_fails(
        self,
        mock_getcwd,
        mock_chdir,
        mock_dlt,
        mock_console,
        mock_get_metadata,
        tmp_path,
        monkeypatch,
    ):
        """Mirrors the native regression test: pipeline.drop() raises, and the
        local cache (keyed on source_name here) must still be gone afterward."""
        pipeline_dir = self._make_stale_cache_dir(tmp_path, monkeypatch)
        mock_get_metadata.return_value = {"dlt_package": "pkg", "dlt_function": "func"}

        runner = self._make_runner(tmp_path)
        self._stub_runner_methods(runner)
        config = self._make_source_config()

        mock_pipeline = MagicMock()
        mock_pipeline.drop.side_effect = RuntimeError("simulated pipeline.drop() failure")
        mock_dlt.pipeline.return_value = mock_pipeline

        result = runner._run_dlt_source(config, full_refresh=True)

        assert result["status"] == "success"
        assert not pipeline_dir.exists(), (
            "local dlt pipeline cache survived a failed pipeline.drop() call in "
            "_run_dlt_source — _clear_local_pipeline_cache() did not run "
            "(or did not fail-safe)"
        )
        assert mock_dlt.pipeline.call_count == 2

    @patch("dango.ingestion.dlt_runner.get_source_metadata")
    @patch("dango.ingestion.dlt_runner.console")
    @patch("dango.ingestion.dlt_runner.dlt")
    @patch("os.chdir")
    @patch("os.getcwd", return_value="/tmp")
    def test_non_full_refresh_sync_never_touches_local_pipeline_cache(
        self,
        mock_getcwd,
        mock_chdir,
        mock_dlt,
        mock_console,
        mock_get_metadata,
        tmp_path,
        monkeypatch,
    ):
        pipeline_dir = self._make_stale_cache_dir(tmp_path, monkeypatch)
        mock_get_metadata.return_value = {"dlt_package": "pkg", "dlt_function": "func"}

        runner = self._make_runner(tmp_path)
        self._stub_runner_methods(runner)
        runner._clear_local_pipeline_cache = MagicMock()
        config = self._make_source_config()

        mock_pipeline = MagicMock()
        mock_dlt.pipeline.return_value = mock_pipeline

        result = runner._run_dlt_source(config, full_refresh=False)

        assert result["status"] == "success"
        runner._clear_local_pipeline_cache.assert_not_called()
        assert pipeline_dir.exists()
        assert mock_dlt.pipeline.call_count == 1


@pytest.mark.unit
class TestClearLocalPipelineCacheCallSites:
    """Structural checks that both full_refresh blocks call the new helper
    with the correct, in-scope variable name (pipeline_name vs source_name)."""

    def test_native_source_calls_with_pipeline_name(self):
        import inspect

        from dango.ingestion.dlt_runner import DltPipelineRunner

        source = inspect.getsource(DltPipelineRunner._run_dlt_native_source)
        assert "self._clear_local_pipeline_cache(pipeline_name)" in source
        drop_pos = source.find("pipeline.drop()")
        clear_pos = source.find("self._clear_local_pipeline_cache(pipeline_name)")
        assert 0 <= drop_pos < clear_pos

    def test_dlt_source_calls_with_source_name(self):
        """_run_dlt_source has no separate pipeline_name variable — its dlt
        pipeline is always named after source_name (see dlt.pipeline(
        pipeline_name=source_name, ...) in that method)."""
        import inspect

        from dango.ingestion.dlt_runner import DltPipelineRunner

        source = inspect.getsource(DltPipelineRunner._run_dlt_source)
        assert "self._clear_local_pipeline_cache(source_name)" in source
        assert "self._clear_local_pipeline_cache(pipeline_name)" not in source
        drop_pos = source.find("pipeline.drop()")
        clear_pos = source.find("self._clear_local_pipeline_cache(source_name)")
        assert 0 <= drop_pos < clear_pos
