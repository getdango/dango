"""tests/unit/test_wp20r_review_r2.py

Reserved backup-style names, unreadable docker labels, drive-relative names, and the full-refresh clear ordering.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from dango.cli.commands.cleanup import cleanup
from dango.cli.commands.docker_audit import docker_audit
from dango.config.models import DataSource, DltNativeConfig, SourceType
from dango.ingestion.dlt_runner import DltPipelineRunner, _checked_pipeline_dir
from dango.platform.docker import get_compose_project_name
from dango.utils.safe_names import contained_child

_PIPELINES = "dango.ingestion.dlt_runner._dlt_pipelines_dir"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_RESERVED = "x_backup_20260101_120000"


def _out(result) -> str:
    return " ".join(_ANSI_RE.sub("", result.output).split())


@pytest.mark.unit
def test_a_pipeline_name_shaped_like_a_state_backup_is_refused_with_a_clear_message(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="reserved"):
        DltNativeConfig(pipeline_name=_RESERVED)
    with pytest.raises(ValueError, match="reserved"):
        DataSource(name=_RESERVED, type=SourceType.CHESS)
    with patch(_PIPELINES, return_value=tmp_path), pytest.raises(ValueError, match="reserved"):
        _checked_pipeline_dir(_RESERVED)


@pytest.mark.unit
@pytest.mark.parametrize("name", ["orders_backup_v2", "orders_backup_2026", "x_backup_2026010_12"])
def test_names_that_only_look_a_little_like_a_backup_are_accepted(name: str) -> None:
    assert DltNativeConfig(pipeline_name=name).pipeline_name == name
    assert DataSource(name=name, type=SourceType.CHESS).name == name


@pytest.mark.unit
def test_an_existing_project_with_such_a_name_gets_the_message_not_a_traceback(
    tmp_path: Path, sample_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.main import cli
    from dango.config.helpers import save_config

    sample_config.sources.sources = []
    save_config(sample_config, tmp_path)
    (tmp_path / ".dango" / "sources.yml").write_text(
        f"version: '1.0'\nsources:\n  - name: {_RESERVED}\n    type: chess\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["source", "list"])
    assert result.exit_code != 0
    assert "reserved" in _out(result)
    assert "Traceback" not in result.output


@pytest.mark.unit
@pytest.mark.parametrize("name", ["D:evil", "C:", "a:b"])
def test_a_drive_relative_name_is_refused(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        contained_child(tmp_path, name)


@pytest.mark.unit
def test_a_nul_byte_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        contained_child(tmp_path, "a\x00b")


def _compose(tmp_path: Path) -> str:
    (tmp_path / "docker-compose.yml").write_text("volumes: [unclosed", encoding="utf-8")
    return get_compose_project_name(tmp_path)


@pytest.mark.unit
def test_the_message_for_an_unreadable_compose_file_says_so(tmp_path: Path) -> None:
    prefix = _compose(tmp_path)

    def run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        if argv[:3] == ["docker", "volume", "ls"]:
            return MagicMock(returncode=0, stdout=f"{prefix}_metabase-data\n")
        return MagicMock(returncode=0, stdout="")

    with (
        patch("subprocess.run", side_effect=run),
        patch("dango.cli.utils.require_project_context", return_value=tmp_path),
    ):
        result = CliRunner().invoke(
            cleanup, ["--docker", "--yes"], obj={"project_root": str(tmp_path)}
        )
    out = _out(result)
    assert "docker-compose.yml could not be read" in out
    assert "every Docker volume" in out
    assert "declared by this project" not in out


def _audit(tmp_path: Path, volume_inspect_stdout: str, volumes: str):
    project = "dango-aaaa1111"
    moved = str(tmp_path / "old-location")  # never created
    line = f"abc\t{project}-metabase-1\t{project}\t{moved}\n"

    def run(argv: list[str], *_a: object, **_k: object) -> MagicMock:
        if argv[:2] == ["docker", "ps"]:
            return MagicMock(returncode=0, stdout=line)
        if argv[:3] == ["docker", "volume", "ls"]:
            return MagicMock(returncode=0, stdout=volumes)
        if argv[:3] == ["docker", "volume", "inspect"]:
            return MagicMock(returncode=0, stdout=volume_inspect_stdout)
        return MagicMock(returncode=0, stdout="")

    with (
        patch("subprocess.run", side_effect=run),
        patch("dango.cli.utils.find_project_root", side_effect=Exception("outside a project")),
    ):
        return CliRunner().invoke(docker_audit, ["--dry-run"])


@pytest.mark.unit
@pytest.mark.parametrize("inspect_stdout", ["", "[]", json.dumps([{"Name": "x", "Labels": None}])])
def test_unreadable_or_missing_labels_on_a_metabase_volume_are_never_safe_to_clean(
    tmp_path: Path, inspect_stdout: str
) -> None:
    result = _audit(tmp_path, inspect_stdout, "dango-aaaa1111_metabase-data\n")
    out = _out(result)
    assert "Orphaned" not in out
    assert "1 group(s) need attention" in out


@pytest.mark.unit
def test_a_group_without_any_volume_is_still_classified_orphaned(tmp_path: Path) -> None:
    result = _audit(tmp_path, "", "")
    assert "Orphaned" in _out(result)


# --- full-refresh clear ordering -------------------------------------------------------


def _runner(tmp_path: Path) -> DltPipelineRunner:
    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    runner.project_root = tmp_path
    runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
    runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    runner._current_oauth_warning = None
    return runner


def _staging_args() -> dict:
    return {
        "source": MagicMock(),
        "source_name": "orders",
        "dataset_name": "raw_orders",
        "pipeline_name": "orders",
        "max_lock_wait": 1,
        "uses_replace_mode": False,
    }


@pytest.mark.unit
def test_staging_reload_does_not_start_when_the_old_staging_cache_cannot_be_cleared(
    tmp_path: Path,
) -> None:
    pipelines = tmp_path / "pipelines"
    (pipelines / "orders_fullrefresh_staging").mkdir(parents=True)
    runner = _runner(tmp_path)
    dlt_mock = MagicMock()
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=OSError("file in use")),
        patch("dango.ingestion.dlt_runner._connect_with_lock_retry"),
        patch("dango.ingestion.dlt_runner.console"),
        patch("dango.ingestion.dlt_runner.dlt", dlt_mock),
        pytest.raises(RuntimeError, match="stopped before loading"),
    ):
        runner._full_refresh_via_staging(**_staging_args())
    dlt_mock.pipeline.assert_not_called()


@pytest.mark.unit
def test_a_cleanup_failure_after_a_failed_staging_reload_does_not_hide_that_failure(
    tmp_path: Path,
) -> None:
    pipelines = tmp_path / "pipelines"
    staging_dir = pipelines / "orders_fullrefresh_staging"
    staging_dir.mkdir(parents=True)
    runner = _runner(tmp_path)
    runner._run_extract_with_retry = MagicMock(side_effect=RuntimeError("extract boom"))
    import shutil

    real_rmtree = shutil.rmtree
    calls: list[Path] = []

    def rmtree(path, *args, **kwargs):
        calls.append(Path(path))
        if len(calls) == 1:
            return real_rmtree(path, *args, **kwargs)  # the pre-start clean slate works
        raise OSError("file in use")

    dlt_mock = MagicMock()
    dlt_mock.pipeline.side_effect = lambda **_k: staging_dir.mkdir(exist_ok=True) or MagicMock()
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=rmtree),
        patch("dango.ingestion.dlt_runner._connect_with_lock_retry"),
        patch("dango.ingestion.dlt_runner.console"),
        patch("dango.ingestion.dlt_runner.dlt", dlt_mock),
    ):
        result = runner._full_refresh_via_staging(**_staging_args())
    assert result["status"] == "failed"
    assert "extract boom" in result["error"]
    assert len(calls) == 2


def _native_config() -> MagicMock:
    config = MagicMock()
    config.name = "test_source"
    config.type.value = "dlt_native"
    config.dlt_native.source_module = "test_module"
    config.dlt_native.source_function = "test_func"
    config.dlt_native.pipeline_name = "test_pipeline"
    config.dlt_native.dataset_name = "raw_test"
    config.dlt_native.source_args = {}
    return config


def _seed_state(pipelines: Path, name: str) -> None:
    (pipelines / name).mkdir(parents=True)
    (pipelines / name / "state.json").write_text('{"cursor": "stale"}', encoding="utf-8")


@pytest.mark.unit
def test_native_full_refresh_restores_state_when_the_clear_fails(tmp_path: Path) -> None:
    pipelines = tmp_path / "pipelines"
    _seed_state(pipelines, "test_pipeline")
    runner = _runner(tmp_path)
    runner._detect_write_disposition = MagicMock(return_value=True)
    runner._restore_dlt_state = MagicMock()
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=OSError("file in use")),
        patch("dango.ingestion.dlt_runner.console"),
        patch("dango.ingestion.dlt_runner.dlt"),
        patch("dango.ingestion.dlt_runner.importlib"),
        patch("os.chdir"),
        patch("os.getcwd", return_value="/tmp"),
        pytest.raises(RuntimeError),
    ):
        runner._run_dlt_native_source(_native_config(), full_refresh=True)
    (restored,) = runner._restore_dlt_state.call_args.args
    assert restored.name.startswith("test_pipeline_backup_")


@pytest.mark.unit
def test_source_full_refresh_restores_state_when_the_clear_fails(tmp_path: Path) -> None:
    pipelines = tmp_path / "pipelines"
    _seed_state(pipelines, "test_source2")
    runner = _runner(tmp_path)
    runner._build_source_config = MagicMock(return_value={})
    runner._check_oauth_token_expiry = MagicMock(return_value=None)
    runner._inject_oauth_credentials = MagicMock(side_effect=lambda _type, kwargs: kwargs)
    runner._get_dataset_name = MagicMock(return_value="raw_test_source2")
    runner._load_dlt_source = MagicMock(return_value=MagicMock())
    runner._detect_write_disposition = MagicMock(return_value=True)
    runner._restore_dlt_state = MagicMock()
    config = MagicMock()
    config.name = "test_source2"
    with (
        patch(_PIPELINES, return_value=pipelines),
        patch("shutil.rmtree", side_effect=OSError("file in use")),
        patch(
            "dango.ingestion.dlt_runner.get_source_metadata",
            return_value={"dlt_package": "pkg", "dlt_function": "func"},
        ),
        patch("dango.ingestion.dlt_runner.console"),
        patch("dango.ingestion.dlt_runner.dlt"),
        patch("os.chdir"),
        patch("os.getcwd", return_value="/tmp"),
        pytest.raises(RuntimeError),
    ):
        runner._run_dlt_source(config, full_refresh=True)
    (restored,) = runner._restore_dlt_state.call_args.args
    assert restored.name.startswith("test_source2_backup_")
