"""tests/unit/test_min_free_disk_gb.py

The minimum free disk space a sync needs is the project setting
`platform.min_free_disk_gb` (default 5, 0 disables), validated at config load,
read by the runner and named in the failure message.
"""

from __future__ import annotations

import importlib
from collections import namedtuple
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from dango.cli.commands.doctor import doctor
from dango.cli.commands.source import sync
from dango.config.exceptions import ConfigValidationError
from dango.config.loader import ConfigLoader
from dango.config.models import PlatformSettings, SourceType
from dango.exceptions import DiskSpaceError
from dango.ingestion.dlt_runner import DltPipelineRunner

# dango.utils exports a function named like some submodules; use the real module for patching
_db_health = importlib.import_module("dango.utils.db_health")

pytestmark = pytest.mark.unit

GIB = 1024**3
KEY = "min_free_disk_gb"
_Usage = namedtuple("_Usage", "total used free")


def _disk(free_gb: float) -> Any:
    """Patch shutil.disk_usage (as db_health sees it) to report *free_gb* free."""
    return patch.object(
        _db_health.shutil,
        "disk_usage",
        return_value=_Usage(500 * GIB, int((500 - free_gb) * GIB), int(free_gb * GIB)),
    )


def _project(root: Path, platform: dict[str, Any] | None = None) -> Path:
    """Write a real project.yml / sources.yml fixture and return the project root."""
    dango_dir = root / ".dango"
    dango_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "project": {"name": "Disk Test", "created_by": "t@example.com", "purpose": "tests"}
    }
    if platform is not None:
        data["platform"] = platform
    (dango_dir / "project.yml").write_text(yaml.safe_dump(data), encoding="utf-8")
    sources = {
        "version": "1.0",
        "sources": [{"name": "csv_src", "type": "csv", "csv": {"directory": "data/csv"}}],
    }
    (dango_dir / "sources.yml").write_text(yaml.safe_dump(sources), encoding="utf-8")
    return root


def _runner(root: Path) -> DltPipelineRunner:
    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    runner.project_root = root
    runner.duckdb_path = root / "data" / "warehouse.duckdb"
    runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    runner._current_oauth_warning = None
    return runner


def _source(kind: str) -> MagicMock:
    src = MagicMock()
    src.name = f"{kind}_src"
    src.enabled = True
    src.empty_sync_policy = "block"
    src.type = {
        "csv": SourceType.CSV,
        "dlt": SourceType.HUBSPOT,
        "dlt_native": SourceType.DLT_NATIVE,
    }[kind]
    return src


class TestSetting:
    def test_default_is_five(self) -> None:
        assert PlatformSettings().min_free_disk_gb == 5

    def test_missing_key_in_project_yml_defaults_to_five(self, tmp_path: Path) -> None:
        platform = ConfigLoader(_project(tmp_path)).load_config().platform
        assert platform.min_free_disk_gb == 5

    @pytest.mark.parametrize("value", [1, 0.5, 0, 12.25])
    def test_loaded_from_project_yml(self, tmp_path: Path, value: float) -> None:
        root = _project(tmp_path, {KEY: value})
        assert ConfigLoader(root).load_config().platform.min_free_disk_gb == value

    @pytest.mark.parametrize(
        "value", [-1, -0.5, "abc", "2GB", "", None, True, float("nan"), float("inf")]
    )
    def test_invalid_value_rejected_at_load_naming_the_key(
        self, tmp_path: Path, value: object
    ) -> None:
        root = _project(tmp_path, {KEY: value})
        with pytest.raises(ConfigValidationError, match=f"platform.{KEY}"):
            ConfigLoader(root).load_config()

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("'2'", 2.0),
            ("'0.5'", 0.5),
            ("' 3 '", 3.0),
            ("1e-9", 1e-9),
            ("5e-1", 0.5),
            ("1E3", 1000),
        ],
    )
    def test_numeric_strings_and_yaml_exponent_floats_are_accepted(
        self, tmp_path: Path, raw: str, expected: float
    ) -> None:
        """Quoted numbers and exponent floats PyYAML reads as strings behave like other keys."""
        root = _project(tmp_path)
        text = f"project:\n  name: T\n  created_by: t@example.com\n  purpose: p\nplatform:\n  {KEY}: {raw}\n"
        (root / ".dango" / "project.yml").write_text(text, encoding="utf-8")
        assert ConfigLoader(root).load_config().platform.min_free_disk_gb == expected

    @pytest.mark.parametrize("raw", ["'true'", "'nan'", "'inf'", "'-2'", "'1e999'", "'2GB'"])
    def test_non_finite_negative_or_non_numeric_strings_still_rejected(
        self, tmp_path: Path, raw: str
    ) -> None:
        root = _project(tmp_path)
        text = f"project:\n  name: T\n  created_by: t@example.com\n  purpose: p\nplatform:\n  {KEY}: {raw}\n"
        (root / ".dango" / "project.yml").write_text(text, encoding="utf-8")
        with pytest.raises(ConfigValidationError, match=f"platform.{KEY}"):
            ConfigLoader(root).load_config()

    @pytest.mark.parametrize("platform_text", ["5", "[1, 2]", "abc"])
    def test_platform_section_that_is_not_a_mapping_is_a_clear_config_error(
        self, tmp_path: Path, platform_text: str
    ) -> None:
        root = _project(tmp_path)
        text = f"project:\n  name: T\n  created_by: t@example.com\n  purpose: p\nplatform: {platform_text}\n"
        (root / ".dango" / "project.yml").write_text(text, encoding="utf-8")
        with pytest.raises(ConfigValidationError, match="'platform' section.*mapping") as exc:
            ConfigLoader(root).load_config()
        assert "project.yml" in str(exc.value)

    def test_invalid_value_rejected_by_the_model(self) -> None:
        with pytest.raises(ValueError, match=KEY):
            PlatformSettings(min_free_disk_gb=-1)

    def test_load_platform_settings_reads_only_the_platform_section(self, tmp_path: Path) -> None:
        root = _project(tmp_path, {KEY: 2})
        (root / ".dango" / "sources.yml").write_text("not: [valid", encoding="utf-8")
        assert ConfigLoader(root).load_platform_settings().min_free_disk_gb == 2

    def test_saved_project_yml_shows_the_key(self, tmp_path: Path) -> None:
        """A freshly written project.yml lists the setting so users can find it."""
        from tests.factories.config_factories import make_dango_config

        loader = ConfigLoader(tmp_path)
        (tmp_path / ".dango").mkdir()
        loader.save_config(make_dango_config())
        saved = yaml.safe_load(loader.project_file.read_text(encoding="utf-8"))
        assert saved["platform"][KEY] == 5


class TestCheckDiskSpace:
    def test_three_gb_free_passes_when_minimum_is_one(self) -> None:
        with _disk(3):
            assert _db_health.check_disk_space(Path("."), min_free_gb=1) is True

    def test_half_gb_free_fails_when_minimum_is_one_and_names_the_key(self) -> None:
        with _disk(0.5), pytest.raises(DiskSpaceError) as exc:
            _db_health.check_disk_space(Path("."), min_free_gb=1)
        text = str(exc.value)
        assert "platform.min_free_disk_gb" in text
        assert ".dango/project.yml" in text
        assert "safety margin" in text
        assert "DuckDB" in text and "dlt" in text
        assert "minimum 1GB" in text

    def test_fractional_minimum(self) -> None:
        with _disk(0.6):
            assert _db_health.check_disk_space(Path("."), min_free_gb=0.5) is True
        with _disk(0.4), pytest.raises(DiskSpaceError, match="minimum 0.5GB"):
            _db_health.check_disk_space(Path("."), min_free_gb=0.5)

    @pytest.mark.parametrize(
        ("free_gb", "minimum", "shown_free"),
        [(0.47, 0.5, "0.47"), (0.96, 1, "0.96"), (4.999, 5, "4.99")],
    )
    def test_free_figure_is_rounded_down_so_the_message_never_contradicts_itself(
        self, free_gb: float, minimum: float, shown_free: str
    ) -> None:
        with _disk(free_gb), pytest.raises(DiskSpaceError) as exc:
            _db_health.check_disk_space(Path("."), min_free_gb=minimum)
        assert f"{shown_free}GB free (minimum {minimum:g}GB required)" in str(exc.value)

    @pytest.mark.parametrize(
        ("free_gb", "minimum", "warns"),
        [
            (8, 1, False),
            (1.5, 1, True),
            (0.9, 0.5, True),
            (1.1, 0.5, False),
            (9, 5, True),
            (11, 5, False),
            (30, 20, True),
            (45, 20, False),
        ],
    )
    def test_low_disk_warning_scales_with_the_setting(
        self, free_gb: float, minimum: float, warns: bool
    ) -> None:
        """Warn below twice the minimum: the default 5 keeps today's 10 GB."""
        with _disk(free_gb), patch.object(_db_health.console, "print") as printed:
            assert _db_health.check_disk_space(Path("."), min_free_gb=minimum) is True
        assert printed.called is warns

    def test_zero_disables_the_check(self) -> None:
        with _disk(0) as usage, patch.object(_db_health.console, "print") as printed:
            assert _db_health.check_disk_space(Path("."), min_free_gb=0) is True
        usage.assert_not_called()
        printed.assert_not_called()

    def test_default_minimum_is_five_and_message_names_the_key(self) -> None:
        with _disk(3), pytest.raises(DiskSpaceError, match="platform.min_free_disk_gb") as exc:
            _db_health.check_disk_space(Path("."))
        assert "minimum 5GB" in str(exc.value)


@pytest.mark.parametrize("kind", ["dlt", "dlt_native", "csv"])
class TestRunSourceUsesTheSetting:
    """run_source() holds the one preflight shared by every source path."""

    def _run(self, root: Path, kind: str, free_gb: float) -> tuple[dict[str, Any], MagicMock]:
        runner = _runner(root)
        dispatched = {"status": "success", "rows_loaded": 0}
        with (
            _disk(free_gb),
            patch.object(runner, "_check_dependencies", return_value=(True, [])),
            patch.object(runner, "_run_with_timeout", return_value=dispatched) as with_timeout,
            patch.object(runner, "_run_csv_source", return_value=dispatched) as csv_loader,
            patch.object(_db_health, "check_duckdb_health", return_value={"status": "new"}),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.utils.sync_history.save_sync_history_entry"),
        ):
            result = runner.run_source(_source(kind))
        return result, (csv_loader if kind == "csv" else with_timeout)

    def test_override_lets_three_gb_through(self, tmp_path: Path, kind: str) -> None:
        result, dispatch = self._run(_project(tmp_path, {KEY: 1}), kind, free_gb=3)
        assert result["status"] == "success", result
        dispatch.assert_called_once()

    def test_below_the_override_is_blocked_with_the_key_in_the_error(
        self, tmp_path: Path, kind: str
    ) -> None:
        result, dispatch = self._run(_project(tmp_path, {KEY: 1}), kind, free_gb=0.5)
        assert result["status"] == "failed"
        assert "platform.min_free_disk_gb" in result["error"]
        assert "minimum 1GB" in result["error"]
        dispatch.assert_not_called()

    def test_zero_disables(self, tmp_path: Path, kind: str) -> None:
        result, dispatch = self._run(_project(tmp_path, {KEY: 0}), kind, free_gb=0.1)
        assert result["status"] == "success", result
        dispatch.assert_called_once()

    def test_default_still_blocks_three_gb(self, tmp_path: Path, kind: str) -> None:
        result, dispatch = self._run(_project(tmp_path), kind, free_gb=3)
        assert result["status"] == "failed"
        assert "minimum 5GB" in result["error"]
        dispatch.assert_not_called()

    def test_project_without_project_yml_uses_the_default(self, tmp_path: Path, kind: str) -> None:
        result, dispatch = self._run(tmp_path, kind, free_gb=3)
        assert result["status"] == "failed"
        assert "minimum 5GB" in result["error"]
        dispatch.assert_not_called()

    def test_invalid_setting_fails_the_source_naming_the_key(
        self, tmp_path: Path, kind: str
    ) -> None:
        result, dispatch = self._run(_project(tmp_path, {KEY: -3}), kind, free_gb=50)
        assert result["status"] == "failed"
        assert "platform.min_free_disk_gb" in result["error"]
        dispatch.assert_not_called()

    @pytest.mark.parametrize(
        "content",
        [
            b"project: [unclosed\n",
            b"\xff\xfe\x00 not utf-8",
            b"project:\n  name: T\n  created_by: t@example.com\n  purpose: p\nplatform: 5\n",
            b"project:\n  name: T\n  created_by: t@example.com\n  purpose: p\nplatform: [1]\n",
        ],
        ids=["bad_yaml", "not_utf8", "platform_int", "platform_list"],
    )
    def test_unreadable_or_malformed_project_yml_fails_the_source_without_raising(
        self, tmp_path: Path, kind: str, content: bytes
    ) -> None:
        root = _project(tmp_path)
        (root / ".dango" / "project.yml").write_bytes(content)
        result, dispatch = self._run(root, kind, free_gb=50)
        assert result["status"] == "failed"
        assert result["error"]
        dispatch.assert_not_called()


class TestSyncCli:
    """`dango sync` end to end through CliRunner with a mocked disk size."""

    def _invoke(self, root: Path, free_gb: float) -> tuple[Any, MagicMock]:
        csv_loader = MagicMock(return_value={"status": "failed", "error": "stub loader reached"})
        with (
            _disk(free_gb),
            patch("dango.cli.utils.require_project_context", return_value=root),
            patch("dango.cli.utils.check_v01x_project"),
            patch("dango.cli.utils.check_git_branch_warning"),
            patch("dango.cli.scaffold.enforce_scaffold"),
            patch("dango.oauth.validation.validate_before_sync"),
            patch.object(DltPipelineRunner, "_check_dependencies", return_value=(True, [])),
            patch.object(DltPipelineRunner, "_run_csv_source", csv_loader),
            patch.object(_db_health, "check_duckdb_health", return_value={"status": "new"}),
            patch("dango.utils.activity_log.log_activity"),
            patch("dango.utils.sync_history.save_sync_history_entry"),
        ):
            result = CliRunner().invoke(sync, ["--yes"], obj={"project_root": str(root)})
        return result, csv_loader

    def test_override_lets_the_loader_run(self, tmp_path: Path) -> None:
        result, csv_loader = self._invoke(_project(tmp_path, {KEY: 1}), free_gb=3)
        csv_loader.assert_called_once()
        assert "Insufficient disk space" not in result.output

    def test_below_the_override_stops_before_the_loader_and_names_the_key(
        self, tmp_path: Path
    ) -> None:
        result, csv_loader = self._invoke(_project(tmp_path, {KEY: 1}), free_gb=0.5)
        assert result.exit_code != 0
        csv_loader.assert_not_called()
        assert "Insufficient disk space" in result.output
        assert "platform.min_free_disk_gb" in result.output
        assert ".dango/project.yml" in result.output

    def test_default_blocks_three_gb(self, tmp_path: Path) -> None:
        result, csv_loader = self._invoke(_project(tmp_path), free_gb=3)
        assert result.exit_code != 0
        csv_loader.assert_not_called()
        assert "minimum 5GB" in result.output

    def test_invalid_setting_aborts_with_the_key_in_the_message(self, tmp_path: Path) -> None:
        result, csv_loader = self._invoke(_project(tmp_path, {KEY: "abc"}), free_gb=50)
        assert result.exit_code != 0
        csv_loader.assert_not_called()
        assert "platform.min_free_disk_gb" in result.output


class TestDoctorWithInvalidPlatformSection:
    """`dango doctor` reports a bad platform value as an error, not a traceback."""

    @pytest.mark.parametrize(
        "platform", [{KEY: -1}, {"port": "abc"}, {KEY: "abc"}], ids=["min_free", "port", "abc"]
    )
    def test_prints_the_error_and_aborts(self, tmp_path: Path, platform: dict[str, Any]) -> None:
        root = _project(tmp_path, platform)
        with patch("dango.cli.utils.require_project_context", return_value=root):
            result = CliRunner().invoke(doctor, [], obj={"project_root": str(root)})
        assert result.exit_code == 1
        assert isinstance(result.exception, SystemExit), repr(result.exception)
        assert "Traceback" not in result.output
        assert "Invalid platform configuration" in result.output
