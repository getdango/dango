"""tests/unit/test_cli_failure_output.py

CLI commands that abort on purpose must not print a blank "Error:" line before "Aborted!".
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner
from rich.console import Console

from dango.cli.commands.source import sync
from tests.unit.test_cli_sync import _patch_sync_prereqs

pytestmark = pytest.mark.unit

_BLANK_ERROR = re.compile(r"^\s*Error:\s*$", re.MULTILINE)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_OK_SUMMARY = {"failed_count": 0, "success_count": 1, "oauth_warnings": []}


def _failed_summary(error: str = "boom: connection refused") -> dict[str, Any]:
    return {
        "failed_count": 1,
        "success_count": 1,
        "success_sources": ["good_src"],
        "failed_sources": [{"name": "bad_src", "error": error, "error_type": "RuntimeError"}],
        "results": [
            {"source": "good_src", "status": "success"},
            {"source": "bad_src", "status": "failed", "error": error},
        ],
        "oauth_warnings": [],
    }


def _plain(result: Any) -> Any:
    return SimpleNamespace(exit_code=result.exit_code, output=_ANSI.sub("", result.output))


def _invoke_sync(args: list[str], run_sync_mock: Any) -> Any:
    patches = _patch_sync_prereqs()
    patches += [
        patch("dango.ingestion.run_sync", run_sync_mock),
        patch("dango.oauth.validation.validate_before_sync"),
        patch(
            "dango.visualization.metabase.refresh_metabase_connection",
            return_value=(False, None, None),
        ),
        patch("dango.visualization.metabase.sync_metabase_schema", return_value=False),
    ]
    for p in patches:
        p.start()
    try:
        return _plain(CliRunner().invoke(sync, args, obj={"project_root": "/tmp"}))
    finally:
        for p in patches:
            p.stop()


class TestSyncFailureOutput:
    def test_failed_source_is_named_with_reason(self) -> None:
        result = _invoke_sync(["--yes"], MagicMock(return_value=_failed_summary()))
        assert result.exit_code == 1
        assert not _BLANK_ERROR.search(result.output), result.output
        assert "bad_src" in result.output
        assert "connection refused" in result.output
        assert "Updating Metabase" not in result.output
        assert "complete" not in result.output.lower()

    def test_failure_reason_is_redacted(self) -> None:
        err = "GET https://api.example.com/x?api_key=SECRETVALUE123 failed"
        result = _invoke_sync(["--yes"], MagicMock(return_value=_failed_summary(err)))
        assert result.exit_code == 1
        assert "SECRETVALUE123" not in result.output

    def test_failure_reason_markup_is_literal(self) -> None:
        result = _invoke_sync(["--yes"], MagicMock(return_value=_failed_summary("bad [/red] tag")))
        assert result.exit_code == 1
        assert "bad [/red] tag" in result.output

    def test_success_unchanged(self) -> None:
        result = _invoke_sync(["--yes"], MagicMock(return_value=dict(_OK_SUMMARY)))
        assert result.exit_code == 0, result.output
        assert "Error" not in result.output
        assert "Aborted" not in result.output

    def test_real_exception_still_prints_message(self) -> None:
        result = _invoke_sync(["--yes"], MagicMock(side_effect=RuntimeError("disk exploded")))
        assert result.exit_code == 1
        assert "Error: disk exploded" in result.output

    def test_keyboard_interrupt_unchanged(self) -> None:
        result = _invoke_sync(["--yes"], MagicMock(side_effect=KeyboardInterrupt()))
        assert result.exit_code == 0
        assert "Sync interrupted" in result.output
        assert "Error" not in result.output

    @pytest.mark.parametrize(
        "args,needle",
        [
            (["--backfill", "7d", "--since", "2024-01-01"], "conflicts"),
            (["--limit", "0"], "positive"),
            (["--since", "2024-12-31", "--until", "2024-01-01"], "before"),
        ],
    )
    def test_validation_aborts_have_no_blank_error(self, args: list[str], needle: str) -> None:
        result = _invoke_sync(args, MagicMock())
        assert result.exit_code == 1
        assert needle in result.output.lower()
        assert not _BLANK_ERROR.search(result.output), result.output

    def test_oauth_prevalidation_abort_has_no_blank_error(self) -> None:
        from dango.exceptions import OAuthTokenExpiredError

        patches = _patch_sync_prereqs()
        patches.append(
            patch(
                "dango.oauth.validation.validate_before_sync",
                side_effect=OAuthTokenExpiredError("token expired for src"),
            )
        )
        for p in patches:
            p.start()
        try:
            result = _plain(CliRunner().invoke(sync, ["--yes"], obj={"project_root": "/tmp"}))
        finally:
            for p in patches:
                p.stop()
        assert result.exit_code == 1
        assert not _BLANK_ERROR.search(result.output), result.output


def _run_cli(cmd: click.Command, args: list[str], root: Path) -> Any:
    with patch("dango.cli.utils.require_project_context", return_value=root):
        return _plain(CliRunner().invoke(cmd, args, obj={"project_root": str(root)}))


class TestOtherAbortSites:
    def test_run_missing_dbt_dir(self, tmp_path: Path) -> None:
        from dango.cli.commands.transform import run

        result = _run_cli(run, [], tmp_path)
        assert result.exit_code == 1
        assert "dbt directory not found" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output

    def test_docs_missing_dbt_dir(self, tmp_path: Path) -> None:
        from dango.cli.commands.transform import docs

        result = _run_cli(docs, [], tmp_path)
        assert result.exit_code == 1
        assert "dbt directory not found" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output

    def test_model_add_cancelled(self, tmp_path: Path) -> None:
        from dango.cli.commands.model import model_add

        with patch("dango.cli.model_wizard.add_model", return_value=None):
            result = _run_cli(model_add, [], tmp_path)
        assert result.exit_code == 1
        assert not _BLANK_ERROR.search(result.output), result.output

    def test_rename_invalid_name(self, tmp_path: Path) -> None:
        from dango.cli.commands.project import rename

        result = _run_cli(rename, ["Bad_Name"], tmp_path)
        assert result.exit_code == 1
        assert "Invalid project name" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output

    def test_config_validate_errors(self, tmp_path: Path) -> None:
        from dango.cli.commands.config_cmd import config_validate

        loader = MagicMock()
        loader.return_value.validate_config.return_value = (False, ["bad key"])
        with patch("dango.config.ConfigLoader", loader):
            result = _run_cli(config_validate, [], tmp_path)
        assert result.exit_code == 1
        assert "bad key" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output

    def test_oauth_remove_missing_credential(self, tmp_path: Path) -> None:
        from dango.cli.commands.oauth import oauth_remove

        storage = MagicMock()
        storage.return_value.get.return_value = None
        with patch("dango.oauth.storage.OAuthStorage", storage):
            result = _run_cli(oauth_remove, ["google_ads"], tmp_path)
        assert result.exit_code == 1
        assert "not found" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output

    @pytest.mark.parametrize(
        "cmd_name", ["oauth_google_ads", "oauth_google_analytics", "oauth_google_sheets"]
    )
    def test_oauth_authenticate_failed(self, tmp_path: Path, cmd_name: str) -> None:
        from dango.cli.commands import oauth as oauth_mod

        provider = MagicMock()
        provider.return_value.authenticate.return_value = None
        with (
            patch("dango.oauth.OAuthManager"),
            patch("dango.oauth.providers.GoogleOAuthProvider", provider),
            patch.object(oauth_mod, "_try_service_account_auth", return_value=False),
        ):
            result = _run_cli(getattr(oauth_mod, cmd_name), [], tmp_path)
        assert result.exit_code == 1
        assert "Authentication failed" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output


_MARKUP_MSG = "[/x] [red]boom"


class TestHandlerMessageIsEscaped:
    """Exception text containing Rich markup must print literally, not crash."""

    def test_sync_real_exception_with_markup(self) -> None:
        result = _invoke_sync(["--yes"], MagicMock(side_effect=RuntimeError(_MARKUP_MSG)))
        assert result.exit_code == 1
        assert "Error: [/x] [red]boom" in result.output
        assert "MarkupError" not in result.output

    def test_model_add_exception_with_markup(self, tmp_path: Path) -> None:
        from dango.cli.commands.model import model_add

        with patch("dango.cli.model_wizard.add_model", side_effect=RuntimeError(_MARKUP_MSG)):
            result = _run_cli(model_add, [], tmp_path)
        assert result.exit_code == 1
        assert "Error: [/x] [red]boom" in result.output

    def test_config_validate_exception_with_markup(self, tmp_path: Path) -> None:
        from dango.cli.commands.config_cmd import config_validate

        loader = MagicMock()
        loader.return_value.validate_config.side_effect = RuntimeError(_MARKUP_MSG)
        with patch("dango.config.ConfigLoader", loader):
            result = _run_cli(config_validate, [], tmp_path)
        assert result.exit_code == 1
        assert "Error: [/x] [red]boom" in result.output


class TestWizardDecline:
    def test_declining_continue_cancels_without_error(self) -> None:
        from dango.cli.source_wizard import SourceWizard

        wizard = SourceWizard(Path("/tmp/fake"))
        meta = {"display_name": "X", "category": "x"}
        with (
            patch.object(SourceWizard, "_print_git_warnings"),
            patch.object(SourceWizard, "_show_source_info"),
            patch.object(SourceWizard, "_get_source_name", return_value="my_src"),
            patch.object(SourceWizard, "_select_source_flat", return_value="csv"),
            patch.object(SourceWizard, "_handle_oauth_setup", return_value="skipped"),
            patch("click.confirm", return_value=False),
            patch("dango.cli.source_wizard.get_source_metadata", return_value=meta),
        ):
            out = StringIO()
            with patch("dango.cli.source_wizard.console", Console(file=out, force_terminal=False)):
                assert wizard.run() is False
        text = out.getvalue()
        assert "Wizard cancelled" in text
        assert "Error" not in text


class TestHelperAbortOutsideProject:
    """require_project_context aborts after printing its own message; no blank Error: after it."""

    @pytest.mark.parametrize(
        "argv",
        [
            ["status"],
            ["info"],
            ["source", "list"],
            ["validate"],
            ["config", "show"],
            ["db", "status"],
            ["oauth", "status"],
            ["oauth", "list"],
            ["stop"],
        ],
    )
    def test_no_blank_error(
        self, argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from dango.cli.main import cli

        home = tmp_path / "home"
        work = tmp_path / "work"
        home.mkdir()
        work.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
        monkeypatch.chdir(work)
        result = _plain(CliRunner().invoke(cli, argv))
        assert result.exit_code == 1, result.output
        assert "Not in a Dango project" in result.output
        assert not _BLANK_ERROR.search(result.output), result.output


class TestWizardCtrlC:
    def test_abort_at_prompt_cancels_without_error(self) -> None:
        from dango.cli.source_wizard import SourceWizard

        wizard = SourceWizard(Path("/tmp/fake"))
        meta = {"display_name": "X", "category": "x"}
        with (
            patch.object(SourceWizard, "_print_git_warnings"),
            patch.object(SourceWizard, "_show_source_info"),
            patch.object(SourceWizard, "_get_source_name", return_value="my_src"),
            patch.object(SourceWizard, "_select_source_flat", return_value="csv"),
            patch.object(SourceWizard, "_handle_oauth_setup", return_value="skipped"),
            patch("click.confirm", side_effect=click.Abort()),
            patch("dango.cli.source_wizard.get_source_metadata", return_value=meta),
        ):
            out = StringIO()
            with patch("dango.cli.source_wizard.console", Console(file=out, force_terminal=False)):
                assert wizard.run() is False
        text = out.getvalue()
        assert "Wizard cancelled" in text
        assert "Error" not in text


class TestMoreEscapedMessages:
    def test_source_edit_invalid_yaml_with_markup(self, tmp_path: Path) -> None:
        from dango.cli.commands.source import source_edit

        dango_dir = tmp_path / ".dango"
        dango_dir.mkdir()
        (dango_dir / "sources.yml").write_text("sources: []\n", encoding="utf-8")
        bad = "sources:\n  - directory: '[/data]' x: 1\n"
        with (
            patch.dict("os.environ", {"EDITOR": "true"}),
            patch("click.edit", return_value=bad),
        ):
            result = _plain(
                CliRunner().invoke(source_edit, [], obj={"project_root": str(tmp_path)})
            )
        assert result.exit_code == 0, result.output
        assert "Invalid YAML" in result.output
        assert "[/data]" in result.output
        assert "Changes NOT saved" in result.output

    def test_start_version_mismatch_with_markup(self, tmp_path: Path) -> None:
        from dango.cli.commands.platform import start
        from dango.exceptions import VersionMismatchError

        config = MagicMock()
        config.project.name = "p"
        loader = MagicMock()
        loader.return_value.load_config.return_value = config
        with (
            patch("dango.cli.utils.check_v01x_project"),
            patch("dango.cli.utils.require_project_context", return_value=tmp_path),
            patch("dango.config.ConfigLoader", loader),
            patch("dango.cli.helpers.process_manager.read_pid_file", return_value=None),
            patch(
                "dango.platform.common.startup.check_duckdb_version_alignment",
                side_effect=VersionMismatchError(_MARKUP_MSG),
            ),
        ):
            result = _plain(CliRunner().invoke(start, [], obj={"project_root": str(tmp_path)}))
        assert result.exit_code == 1
        assert "DuckDB version mismatch" in result.output
        assert "[/x] [red]boom" in result.output
        assert "MarkupError" not in result.output


class TestRemoteUpgradeExit:
    def test_unknown_latest_version_prints_error_once(self) -> None:
        from dango.cli.commands.remote_ops import remote_upgrade

        ssh = MagicMock()
        with (
            patch(
                "dango.cli.commands.remote._load_cloud_config_with_ssh_or_fail",
                return_value=(MagicMock(), ssh),
            ),
            patch("dango.cli.commands.remote_ops.refuse_if_powered_off"),
            patch("dango.platform.cloud.upgrade.check_versions", return_value=("1.0.14", None)),
            patch("dango.platform.cloud.runtime_ownership.at_target_restart_plan"),
        ):
            result = _plain(CliRunner().invoke(remote_upgrade, [], obj={}))
        assert result.exit_code == 1, result.output
        assert "Could not determine latest version" in result.output
        assert "Upgrade failed" not in result.output
        ssh.disconnect.assert_called_once()
