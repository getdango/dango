"""tests/unit/test_source_config_honesty.py

Source configuration is checked where users make mistakes: duplicate or case-colliding names,
the credential variable a source really uses, an invalid `dango source edit`, and misspelled
keys inside a source block.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from dango.cli.main import cli
from dango.cli.validate import ProjectValidator
from dango.config import ConfigLoader
from dango.config.models import SourcesConfig
from dango.exceptions import ConfigValidationError

pytestmark = pytest.mark.unit

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _local(name: str, directory: str = "data/uploads/a", **extra: Any) -> dict[str, Any]:
    block: dict[str, Any] = {"directory": directory, **extra}
    return {"name": name, "type": "local_files", "local_files": block}


def _write_project(root: Path, sources: list[dict[str, Any]]) -> Path:
    dango = root / ".dango"
    dango.mkdir(parents=True, exist_ok=True)
    (dango / "project.yml").write_text(
        yaml.safe_dump(
            {"project": {"name": "t", "created_by": "x@example.com", "purpose": "testing"}}
        ),
        encoding="utf-8",
    )
    (dango / "sources.yml").write_text(
        yaml.safe_dump({"version": "1.0", "sources": sources}), encoding="utf-8", newline="\n"
    )
    return root


def _rows(validator: ProjectValidator, prefix: str) -> list[Any]:
    return [r for r in validator.results if r.name.startswith(prefix)]


def _flat(text: str) -> str:
    return " ".join(_ANSI.sub("", text).split())


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


# ---------------------------------------------------------------------------
# M3: duplicate source names
# ---------------------------------------------------------------------------


class TestDuplicateSourceNames:
    def test_exact_duplicate_is_rejected_with_the_name(self) -> None:
        with pytest.raises(ValidationError) as info:
            SourcesConfig.model_validate(
                {"sources": [_local("sales"), _local("sales", "data/uploads/b")]}
            )
        text = _flat(str(info.value))
        assert "sales" in text and "Rename one" in text

    def test_case_duplicate_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as info:
            SourcesConfig.model_validate(
                {"sources": [_local("sales"), _local("Sales", "data/uploads/b")]}
            )
        assert "sales" in str(info.value)

    def test_every_duplicated_name_is_listed(self) -> None:
        sources = [_local("a"), _local("A"), _local("b"), _local("b"), _local("c")]
        with pytest.raises(ValidationError) as info:
            SourcesConfig.model_validate({"sources": sources})
        assert "Duplicate source name(s): a, b." in _flat(str(info.value))

    def test_distinct_names_are_accepted(self) -> None:
        config = SourcesConfig.model_validate(
            {"sources": [_local("sales"), _local("sales2"), _local("orders")]}
        )
        assert [s.name for s in config.sources] == ["sales", "sales2", "orders"]

    def test_loader_names_the_file_and_the_duplicate(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("Sales", "data/uploads/b")])
        with pytest.raises(ConfigValidationError) as info:
            ConfigLoader(root).load_sources_config()
        text = _flat(str(info.value))
        assert "sources.yml" in text and "sales" in text and "Rename one" in text

    def test_normal_config_still_loads(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("orders", "data/uploads/b")])
        assert len(ConfigLoader(root).load_sources_config().sources) == 2

    def test_source_list_fails_loudly_with_the_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("Sales", "data/uploads/b")])
        monkeypatch.chdir(root)

        result = CliRunner().invoke(cli, ["source", "list"])

        text = _flat(result.output)
        assert result.exit_code == 1, text
        assert "sales" in text and "Rename one" in text

    def test_validate_reports_the_duplicate_without_relying_on_the_truncated_row(
        self, tmp_path: Path
    ) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("Sales", "data/uploads/b")])
        validator = ProjectValidator(root)

        validator._check_source_names()

        rows = _rows(validator, "Source names")
        assert len(rows) == 1 and rows[0].status == "fail"
        assert "sales" in rows[0].message and "rename one" in rows[0].message.lower()

    def test_validate_reports_nothing_for_distinct_names(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("orders", "data/uploads/b")])
        validator = ProjectValidator(root)

        validator._check_source_names()

        assert not _rows(validator, "Source names")

    def test_validate_all_runs_the_source_checks(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("Sales", "data/uploads/b")])
        validator = ProjectValidator(root)
        heavy = (
            "_check_scaffold",
            "_check_dbt_setup",
            "_check_model_sql_portability",
            "_check_script_paths",
            "_check_database",
            "_check_dependencies",
            "_check_permissions",
            "_check_tracked_secrets",
        )
        with patch.multiple(validator, **{name: MagicMock() for name in heavy}):
            summary = validator.validate_all(display=False, include_connectivity=False)

        assert any(r.name == "Source names" and r.status == "fail" for r in summary["results"])
        assert summary["is_valid"] is False

    def test_scheduled_sync_records_a_failure_instead_of_dying_silently(
        self, tmp_path: Path
    ) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("Sales", "data/uploads/b")])
        sp = "dango.platform.sync_process"
        nm = "dango.platform.notifications.webhook"
        jobs = "dango.platform.scheduling.jobs"
        with (
            patch(f"{nm}.load_notification_config", return_value=None),
            patch(f"{nm}.WebhookSender"),
            patch(f"{sp}.launch_sync_subprocess") as launch,
            patch(f"{jobs}._broadcast") as broadcast,
            patch(f"{jobs}._notify"),
            patch(f"{jobs}._try_record_start", return_value=None),
            patch(f"{jobs}._try_finish_record"),
            patch(f"{jobs}._log_execution_event") as logged,
        ):
            from dango.platform.scheduling.jobs import _run_scheduled_sync_impl

            _run_scheduled_sync_impl("daily", ["sales"], project_root=str(root))

        launch.assert_not_called()
        events = [c.args[0]["event"] for c in broadcast.call_args_list]
        assert "sync_failed" in events
        assert logged.call_args.kwargs["status"] == "failed"
        assert "sales" in logged.call_args.kwargs["error"]

    def test_source_edit_still_opens_a_config_with_duplicates_and_saves_the_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(tmp_path, [_local("sales"), _local("Sales", "data/uploads/b")])
        fixed = yaml.safe_dump(
            {"version": "1.0", "sources": [_local("sales"), _local("sales_b", "data/uploads/b")]}
        )
        monkeypatch.chdir(root)
        monkeypatch.setenv("EDITOR", "true")
        monkeypatch.setattr("click.edit", lambda text, **kw: fixed)

        result = CliRunner().invoke(cli, ["source", "edit"])

        assert result.exit_code == 0, result.output
        assert (root / ".dango" / "sources.yml").read_text(encoding="utf-8") == fixed
        assert len(ConfigLoader(root).load_sources_config().sources) == 2


# ---------------------------------------------------------------------------
# L1: validate checks the variable the source really uses
# ---------------------------------------------------------------------------


def _stripe(name: str, **block: Any) -> dict[str, Any]:
    return {"name": name, "type": "stripe", "stripe": block}


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("STRIPE_API_KEY", "PAYMENTS_API_KEY", "PAYMENTS_STRIPE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


class TestEnvVarCheckUsesTheSourcesOwnVariable:
    def test_own_variable_set_gives_no_warning(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(
            tmp_path, [_stripe("payments", stripe_secret_key_env="PAYMENTS_API_KEY")]
        )
        monkeypatch.setenv("PAYMENTS_API_KEY", "x")
        validator = ProjectValidator(root)

        validator._check_env_vars()

        assert not _rows(validator, "Env")

    def test_own_variable_missing_warns_naming_the_real_variable(
        self, tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(
            tmp_path, [_stripe("payments", stripe_secret_key_env="PAYMENTS_API_KEY")]
        )
        monkeypatch.setenv("STRIPE_API_KEY", "registry-default-is-set")
        validator = ProjectValidator(root)

        validator._check_env_vars()

        rows = _rows(validator, "Env: payments")
        assert len(rows) == 1 and rows[0].status == "warn"
        assert "PAYMENTS_API_KEY" in rows[0].message
        assert "STRIPE_API_KEY" not in rows[0].message

    def test_variable_in_the_project_env_file_counts_as_set(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        root = _write_project(
            tmp_path, [_stripe("payments", stripe_secret_key_env="PAYMENTS_API_KEY")]
        )
        (root / ".env").write_text("PAYMENTS_API_KEY=abc\n", encoding="utf-8")
        validator = ProjectValidator(root)

        validator._check_env_vars()

        assert not _rows(validator, "Env")

    def test_default_variable_name_still_checked_when_it_is_the_one_in_use(
        self, tmp_path: Path, clean_env: None
    ) -> None:
        root = _write_project(tmp_path, [_stripe("stripe", stripe_secret_key_env="STRIPE_API_KEY")])
        validator = ProjectValidator(root)

        validator._check_env_vars()

        rows = _rows(validator, "Env: stripe")
        assert len(rows) == 1 and "STRIPE_API_KEY" in rows[0].message

    def test_file_sources_print_no_env_row(self, tmp_path: Path, clean_env: None) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        validator = ProjectValidator(root)

        validator._check_env_vars()

        assert not _rows(validator, "Env")


# ---------------------------------------------------------------------------
# L4: dango source edit validates the schema before it writes
# ---------------------------------------------------------------------------


def _edit(root: Path, monkeypatch: pytest.MonkeyPatch, edited: str) -> Any:
    monkeypatch.chdir(root)
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.setattr("click.edit", lambda text, **kw: edited)
    return CliRunner().invoke(cli, ["source", "edit"])


class TestSourceEditValidatesBeforeWriting:
    def test_invalid_type_is_rejected_and_the_file_is_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        path = root / ".dango" / "sources.yml"
        before = path.read_bytes()
        bad = path.read_text(encoding="utf-8").replace("type: local_files", "type: local_file")

        result = _edit(root, monkeypatch, bad)

        text = _flat(result.output)
        assert result.exit_code == 1, text
        assert path.read_bytes() == before
        assert "updated" not in text
        assert "Changes NOT saved" in text
        assert "type" in text and "local_file" in text

    def test_duplicate_names_are_rejected_and_the_file_is_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        path = root / ".dango" / "sources.yml"
        before = path.read_bytes()
        dup = yaml.safe_dump({"sources": [_local("sales"), _local("Sales", "data/uploads/b")]})

        result = _edit(root, monkeypatch, dup)

        assert result.exit_code == 1, result.output
        assert path.read_bytes() == before
        assert "sales" in _flat(result.output)

    def test_a_document_that_is_not_a_mapping_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        path = root / ".dango" / "sources.yml"
        before = path.read_bytes()

        result = _edit(root, monkeypatch, "- just\n- a list\n")

        assert result.exit_code == 1, result.output
        assert path.read_bytes() == before
        assert "Changes NOT saved" in _flat(result.output)

    def test_a_valid_edit_is_saved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        edited = yaml.safe_dump(
            {"version": "1.0", "sources": [_local("sales"), _local("orders", "data/uploads/o")]}
        )

        result = _edit(root, monkeypatch, edited)

        assert result.exit_code == 0, result.output
        assert (root / ".dango" / "sources.yml").read_text(encoding="utf-8") == edited
        assert "sources.yml updated" in _flat(result.output)


# ---------------------------------------------------------------------------
# L7a: misspelled keys inside a source block warn, and the source still loads
# ---------------------------------------------------------------------------


class TestUnknownKeysInSourceBlocks:
    def test_a_typo_warns_naming_the_source_and_the_key(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales", file_patern="*.txt")])
        validator = ProjectValidator(root)

        validator._check_source_keys()

        rows = _rows(validator, "Source keys: sales")
        assert len(rows) == 1 and rows[0].status == "warn"
        assert "file_patern" in rows[0].message and "local_files" in rows[0].message

    def test_the_source_with_the_typo_still_loads_with_the_default(self, tmp_path: Path) -> None:
        root = _write_project(tmp_path, [_local("sales", file_patern="*.txt")])

        source = ConfigLoader(root).load_sources_config().get_source("sales")

        assert source is not None and source.local_files is not None
        assert source.local_files.file_pattern == "*"

    def test_every_misspelled_key_of_every_source_is_named(self, tmp_path: Path) -> None:
        root = _write_project(
            tmp_path,
            [
                _local("a", file_patern="x", directry="y"),
                _local("b"),
                _stripe("c", stripe_secret_key_envv="K"),
            ],
        )
        validator = ProjectValidator(root)

        validator._check_source_keys()

        names = {r.name for r in _rows(validator, "Source keys")}
        assert names == {"Source keys: a", "Source keys: c"}
        a_row = _rows(validator, "Source keys: a")[0]
        assert "file_patern" in a_row.message and "directry" in a_row.message

    def test_known_keys_and_aliases_do_not_warn(self, tmp_path: Path) -> None:
        root = _write_project(
            tmp_path,
            [
                _local("a", file_pattern="*.csv", notes="n"),
                {"name": "s", "type": "slack", "slack": {"token_env": "T", "channels": ["C1"]}},
                {
                    "name": "n",
                    "type": "dlt_native",
                    "dlt_native": {"source_module": "m", "function_kwargs": {"anything": 1}},
                },
            ],
        )
        validator = ProjectValidator(root)

        validator._check_source_keys()

        assert not _rows(validator, "Source keys")

    def test_a_broken_sources_file_adds_no_key_rows_and_does_not_crash(
        self, tmp_path: Path
    ) -> None:
        root = _write_project(tmp_path, [_local("sales")])
        (root / ".dango" / "sources.yml").write_text("sources: [unclosed\n", encoding="utf-8")
        validator = ProjectValidator(root)

        validator._check_source_keys()
        validator._check_source_names()

        assert not validator.results
