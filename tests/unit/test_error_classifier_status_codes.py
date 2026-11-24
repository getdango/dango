"""tests/unit/test_error_classifier_status_codes.py

Error classifiers read an HTTP status code only as a whole number: a DuckDB lock message whose PID
or path contains 401, 403, 404 or 429 is still a lock error, and real status codes still classify.
"""

from __future__ import annotations

import importlib
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import dlt
import pytest
from rich.console import Console

pytestmark = pytest.mark.unit

# dango.utils exports a function named dbt_lock that shadows the submodule of the same name.
_dbt_lock_module = importlib.import_module("dango.utils.dbt_lock")

_LOCK_TEMPLATE = (
    'IO Error: Could not set lock on file "/data/warehouse.duckdb": Conflicting lock is held in '
    "/usr/bin/python3 (PID {pid}) by user dango. See also https://duckdb.org/docs/connect/concurrency"
)
_WINDOWS_LOCK = (
    'IO Error: Cannot open file "C:\\proj\\data\\warehouse.duckdb": The process cannot access the '
    "file because it is being used by another process. (PID {pid})"
)


@pytest.fixture
def analyze_error() -> Any:
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = DltPipelineRunner.__new__(DltPipelineRunner)
    return runner._analyze_error


class TestLockWinsOverStatusDigits:
    @pytest.mark.parametrize("pid", ["40187", "140387", "1429", "429", "401", "403"])
    def test_unix_lock_message_with_digits_in_the_pid(self, analyze_error: Any, pid: str) -> None:
        result = analyze_error(Exception(_LOCK_TEMPLATE.format(pid=pid)), "my_source")
        assert "DuckDB Lock Error" in result
        assert "Rate Limit Error" not in result
        assert "Authentication Error" not in result

    @pytest.mark.parametrize("pid", ["40187", "4290", "14039"])
    def test_windows_lock_message_with_digits_in_the_pid(
        self, analyze_error: Any, pid: str
    ) -> None:
        result = analyze_error(Exception(_WINDOWS_LOCK.format(pid=pid)), "my_source")
        assert "DuckDB Lock Error" in result

    def test_lock_message_with_digits_in_the_path(self, analyze_error: Any) -> None:
        text = _LOCK_TEMPLATE.format(pid="77").replace("/data/", "/tmp/pytest-4291/data/")
        assert "DuckDB Lock Error" in analyze_error(Exception(text), "my_source")

    @pytest.mark.parametrize("pid", ["40187", "4290", "14039"])
    def test_printed_guidance_is_the_lock_block(self, tmp_path: Path, pid: str) -> None:
        from dango.ingestion import dlt_runner

        runner = object.__new__(dlt_runner.DltPipelineRunner)
        runner.project_root = tmp_path
        buf = StringIO()
        with patch.object(dlt_runner, "console", Console(file=buf, width=300)):
            runner._print_friendly_error(Exception(_LOCK_TEMPLATE.format(pid=pid)), "my_source")
        output = buf.getvalue()
        assert "DuckDB Lock Error" in output
        assert "Rate Limit Error" not in output
        assert "Authentication Error" not in output

    def test_a_lock_message_that_also_carries_a_real_429_is_a_lock_error(
        self, analyze_error: Any
    ) -> None:
        # Choice: DuckDB's lock phrases are specific to DuckDB, so a message that contains one is
        # about the warehouse even if a status code also appears in it.
        text = _LOCK_TEMPLATE.format(pid="77") + " (upstream said HTTP 429)"
        assert "DuckDB Lock Error" in analyze_error(Exception(text), "my_source")

    @pytest.mark.parametrize(
        "text",
        [
            "401 Unauthorized: your account is locked",
            "HTTP 429: the queue is locked, too many requests",
        ],
    )
    def test_a_generic_locked_word_in_a_real_http_error_keeps_its_class(
        self, analyze_error: Any, text: str
    ) -> None:
        result = analyze_error(Exception(text), "my_source")
        assert "DuckDB Lock Error" not in result


class TestStatusCodesStillClassify:
    @pytest.mark.parametrize(
        "text",
        ["HTTP 401", "401 Unauthorized", "403 Forbidden", "status: 403", "error code=401,"],
    )
    def test_authentication(self, analyze_error: Any, text: str) -> None:
        assert "Authentication Error" in analyze_error(Exception(text), "my_source")

    @pytest.mark.parametrize(
        "text",
        ["HTTP 429", "429 Too Many Requests", "status: 429", "rate limit hit", "Throttled"],
    )
    def test_rate_limit(self, analyze_error: Any, text: str) -> None:
        assert "Rate Limit Error" in analyze_error(Exception(text), "my_source")

    @pytest.mark.parametrize(
        "text",
        [
            "invalid api key",
            "authentication failed",
            "Invalid Credentials supplied",
            "unauthorized",
            "forbidden",
        ],
    )
    def test_words_still_match_without_a_code(self, analyze_error: Any, text: str) -> None:
        assert "Authentication Error" in analyze_error(Exception(text), "my_source")

    @pytest.mark.parametrize(
        "text",
        [
            "worker 40187 stopped",
            "see /tmp/pytest-4291/x",
            "row 14039 rejected",
            "request id 1429",
        ],
    )
    def test_digits_inside_longer_numbers_are_not_status_codes(
        self, analyze_error: Any, text: str
    ) -> None:
        result = analyze_error(Exception(text), "my_source")
        assert "Authentication Error" not in result
        assert "Rate Limit Error" not in result


def _wizard_output(message: str, tmp_path: Path) -> str:
    from dango.cli.source_wizard import SourceWizard

    cred = MagicMock()
    cred.credentials = {"refresh_token": "r", "client_id": "c", "client_secret": "s"}
    cred.metadata = {}
    service = MagicMock()
    service.spreadsheets.return_value.get.return_value.execute.side_effect = Exception(message)
    buf = StringIO()
    with (
        patch("googleapiclient.discovery.build", return_value=service),
        patch("google.oauth2.credentials.Credentials"),
        patch("google.auth.transport.requests.Request"),
        patch("dango.cli.source_wizard.OAuthStorage") as storage,
        patch("dango.cli.source_wizard.console", Console(file=buf, width=300)),
    ):
        storage.return_value.get.return_value = cred
        wizard = SourceWizard(tmp_path)
        wizard._current_spreadsheet_id = "sheet-id"
        with pytest.raises(Exception, match="worker|HTTP|status"):
            wizard._fetch_google_sheets("my_sheets")
    return buf.getvalue()


class TestGoogleSheetsWizardClassifier:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("HTTP 404 from sheets", "Spreadsheet not found"),
            ("status: 403", "Permission denied"),
            ("HTTP 401", "OAuth credential expired or invalid"),
        ],
    )
    def test_real_codes_keep_their_message(
        self, tmp_path: Path, message: str, expected: str
    ) -> None:
        assert expected in _wizard_output(message, tmp_path)

    @pytest.mark.parametrize(
        "message",
        [
            "worker 140487 crashed",
            "worker 140387 crashed",
            "worker 140187 crashed",
            "worker crashed in /tmp/pytest-4041/x",
        ],
    )
    def test_digits_inside_a_longer_number_are_not_a_status_code(
        self, tmp_path: Path, message: str
    ) -> None:
        out = _wizard_output(message, tmp_path)
        assert "Error fetching sheets" in out
        assert "Spreadsheet not found" not in out
        assert "Permission denied" not in out
        assert "OAuth credential expired" not in out


def _mock_pipeline() -> Any:
    pipeline = MagicMock(spec=dlt.Pipeline)
    pipeline.dataset_name = "raw_test_source"
    return pipeline


def _resource_tip_shown(message: str, tmp_path: Path) -> bool:
    from dango.ingestion.dlt_runner import DltPipelineRunner

    runner = DltPipelineRunner(tmp_path)
    runner.duckdb_path = tmp_path / "data" / "warehouse.duckdb"
    runner.duckdb_path.parent.mkdir(parents=True, exist_ok=True)
    config = MagicMock()
    config.name = "test_source"
    config.type = MagicMock()
    config.type.value = "hubspot"
    config.enabled = True
    config.csv = None
    config.dlt_native = None
    console = MagicMock()
    with (
        patch("dlt.pipeline", return_value=_mock_pipeline()),
        patch("dango.ingestion.dlt_runner.os.chdir"),
        patch(
            "dango.ingestion.dlt_runner.get_source_metadata",
            return_value={"dlt_package": "dlt.sources.test", "dlt_function": "test_source"},
        ),
        patch.object(_dbt_lock_module, "DbtLock"),
        patch.object(runner, "_load_dlt_source", return_value=MagicMock()),
        patch.object(runner, "_backup_dlt_state", return_value=tmp_path / "backup"),
        patch.object(runner, "_get_source_total_rows", return_value=100),
        patch.object(runner, "_get_source_table_rows", return_value={}),
        patch.object(runner, "_detect_write_disposition", return_value=False),
        patch.object(runner, "_check_oauth_token_expiry", return_value=None),
        patch.object(runner, "_inject_oauth_credentials", return_value={}),
        patch.object(runner, "_get_dataset_name", return_value="raw_test_source"),
        patch.object(runner, "_build_source_config", return_value={}),
        patch.object(runner, "_run_extract_with_retry", side_effect=RuntimeError(message)),
        patch.object(runner, "_cleanup_state_backup"),
        patch.object(runner, "_restore_dlt_state"),
        patch("dango.ingestion.dlt_runner.console", console),
    ):
        with pytest.raises(RuntimeError):  # the failure is re-raised after the guidance prints
            runner._run_dlt_source(config)
    printed = " ".join(str(call.args[0]) for call in console.print.call_args_list if call.args)
    return "higher API permissions" in printed


class TestPerResourceTip:
    @pytest.mark.parametrize("message", ["HTTP 403", "404 Not Found", "forbidden", "status: 404"])
    def test_real_codes_and_words_show_the_tip(self, tmp_path: Path, message: str) -> None:
        assert _resource_tip_shown(message, tmp_path)

    @pytest.mark.parametrize(
        "message",
        ["worker 140387 crashed", "worker 14049 crashed", "crashed in /tmp/pytest-4041/x"],
    )
    def test_digits_inside_a_longer_number_do_not_show_the_tip(
        self, tmp_path: Path, message: str
    ) -> None:
        assert not _resource_tip_shown(message, tmp_path)
