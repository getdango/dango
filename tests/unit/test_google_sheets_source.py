"""tests/unit/test_google_sheets_source.py

Unit tests for google_sheets source empty range handling.
"""

import json
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

# Minimal valid service account JSON, shared by every test class in this
# module — real credential *values* never matter since api_auth() is always
# mocked; dlt just needs something that parses as GCP credentials config.
_CREDS_JSON = {
    "type": "service_account",
    "project_id": "test-project",
    "private_key_id": "key-id",
    "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygvFdB0dShW1x4Y1OfAhXmYy8PqjZb7x\nhQ+YrqFfBhBZhLFmOOZp3qKZdCCT4yy7Ync6uyNvLfW8EFWvWQaQG7ZH5L85bvhH\neSNc7RzMFHKGvCqSZtbfvLsVHZHxg3fOGKf6JR4gFPLo3X9YXs1MZlUCAwEAAQKC\nAQB/Ej4gSmRpSH0wkW+7/DQOVsKFAJ7HpNB/xLxVpJ6qbH3k3q2OqcTcQhCQhvZN\njFXLqPl3qLYl6h7T2s7E7VQxP1+S/JfXUW7CJPE/XfZdD1Y5E1P8/v3fP2YMfH+T\nxM3qM1q9I5ZqM8P4c2RLxKQxq0Z7P2SWXkQF48CqUQKBgQDsEZ5VLbZ4qVVaLqKl\nq3IFjkKvGPx9QVFZ7nU1B8QdkJqjdXKpVx3E7Aw9LLPw6M8YjIVzHfV3Fz2ZWCWa\nGtUxDaD8UL5QQ8PdZjJZVzZ/XBjVLyNLXl8Z5qNvU9LnvLxJwcKPDQpEU8K0S4kQ\nQW8X5Q7L8xXOjBfQjQKBgQDgJnZPP5p8QU5xz8Y5TlG3pxVWJVFVDzN7z7x5Y7Zh\nxW3LH2T0DxV3F5xL5VPUVyNg7L8aFVVqZJjL2p3o6F0q/lXxe8G6Y3QXvZAOqD9l\n0gqX7KpvLjJE+t4fTaYXV5TvFXSE1YY0P8H5r7qYJ0N9W2x8c7mZFzlKwQKBgDHo\nPBxZbsn3RJLx5XvP2X2GF7H/+4b8YN3vQQvVvFh3Zn7nJ7Y0q/dAW8PvP8kXCnJq\nX3qJqCjmEVVQu2F6/qVl6fPY4Z9eNKzMQvE8wR3cKJQ/lLrxLqH8LvLTlH6nJFxP\nLqNOV0c5xJqI3FxWqpHQKlKKvVFQJOYPkZQKBgQCXtx6E1p8uIqJvT7yoMrZJKXL5\nIJCf3BYz7yXd4CVCFMy8Y5E5VZVzEvRh7qY/wFg2Fl3W2n2V4I7Z1qvKI6pQcMBu\nFGLbEWQsVy9mFh6yZxsH8Yw4P1t1k0sY7fJpqB5gQKBgQCsAaHxn5K3dBj5Y1Yx\nJP8T5QQfv+3RY0F8qKBrQBzhANWQUtLl3pj8NkLZFWVqL8yXmCUmKVFfNzOxNgzj\nQ==\n-----END RSA PRIVATE KEY-----",
    "client_email": "test@test-project.iam.gserviceaccount.com",
    "client_id": "1234567890",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
}


@pytest.fixture(autouse=True)
def setup_credentials(monkeypatch):
    """Set up mock Google credentials via environment for every test in this
    module (both the extractor-level and the run_source()-level classes)."""
    monkeypatch.setenv("SOURCES__GOOGLE_SHEETS__CREDENTIALS", json.dumps(_CREDS_JSON))


@pytest.mark.unit
class TestGoogleSheetsEmptyRange:
    """Test that empty/header-only Google Sheets ranges log a warning and are
    skipped for that sync, instead of raising an unconditional RuntimeError.
    The emptiness now flows through as a normal (zero-row) extraction result
    so it can be evaluated by dlt_runner's per-source empty_sync_policy guard."""

    def _make_source(self, all_range_data, range_names=None):
        """Helper to create a source with mocked API calls."""
        from dango.ingestion.dlt_sources.google_sheets import google_spreadsheet

        sheet_names = ["Sheet1"]
        spreadsheet_title = "Test Spreadsheet"

        with (
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_auth",
                return_value=MagicMock(),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_known_range_names",
                return_value=(sheet_names, [], spreadsheet_title),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_data_for_ranges",
                return_value=all_range_data,
            ),
        ):
            source = google_spreadsheet(
                spreadsheet_url_or_id="test-id",
                range_names=range_names or ["MySheet"],
            )
            return list(source)

    @staticmethod
    def _spreadsheet_info_rows(consumed_rows):
        """Filter the `spreadsheet_info` metadata rows out of the flattened
        row stream produced by consuming a `DltSource` with `list(source)`.

        Iterating a `DltSource` directly (as these tests do, without a
        pipeline) yields the flattened *data* rows of every resource, not the
        `DltResource` wrapper objects — the `spreadsheet_info` rows are the
        ones carrying a `range_name` key."""
        return [row for row in consumed_rows if isinstance(row, dict) and "range_name" in row]

    def test_empty_range_skipped_with_warning(self):
        """A range with no data (values=None) logs a warning and is skipped —
        it no longer raises RuntimeError."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), None),  # values=None
        ]
        with patch("dango.ingestion.dlt_sources.google_sheets.logger.warning") as mock_warning:
            resource_list = self._make_source(all_range_data)

        mock_warning.assert_called()
        warning_texts = [str(call) for call in mock_warning.call_args_list]
        assert any("MySheet" in text for text in warning_texts)

        rows = self._spreadsheet_info_rows(resource_list)
        assert any(row["range_name"] == "MySheet" and row["skipped"] for row in rows)

    def test_empty_values_list_skipped_with_warning(self):
        """A range with an empty values list (values=[]) logs a warning and is
        skipped — it no longer raises RuntimeError."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), []),  # values=[]
        ]
        with patch("dango.ingestion.dlt_sources.google_sheets.logger.warning") as mock_warning:
            resource_list = self._make_source(all_range_data)

        mock_warning.assert_called()
        warning_texts = [str(call) for call in mock_warning.call_args_list]
        assert any("MySheet" in text for text in warning_texts)

        rows = self._spreadsheet_info_rows(resource_list)
        assert any(row["range_name"] == "MySheet" and row["skipped"] for row in rows)

    def test_header_only_range_skipped_with_warning(self):
        """A range with only a header row logs a warning and is skipped — it
        no longer raises RuntimeError."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), [["id", "amount"]]),  # header only
        ]
        with patch("dango.ingestion.dlt_sources.google_sheets.logger.warning") as mock_warning:
            resource_list = self._make_source(all_range_data)

        mock_warning.assert_called()
        warning_texts = [str(call) for call in mock_warning.call_args_list]
        assert any("MySheet" in text and "header row" in text for text in warning_texts)

        rows = self._spreadsheet_info_rows(resource_list)
        assert any(row["range_name"] == "MySheet" and row["skipped"] for row in rows)

    def test_warning_message_includes_range_name(self):
        """The warning message (not an error message) includes the range name."""
        all_range_data = [
            ("MySheet", MagicMock(), MagicMock(), []),
        ]
        with patch("dango.ingestion.dlt_sources.google_sheets.logger.warning") as mock_warning:
            self._make_source(all_range_data)

        mock_warning.assert_called()
        warning_texts = [str(call) for call in mock_warning.call_args_list]
        assert any("MySheet" in text for text in warning_texts)
        # The removed RuntimeError text made claims about data preservation that
        # depend on empty_sync_policy, resolved downstream in dlt_runner.py —
        # the extractor itself must not repeat that claim.
        assert not any("preserved" in text for text in warning_texts)

    def test_multi_range_one_empty_others_load(self):
        """A source with two ranges — one empty, one with real data — should
        skip only the empty range and still yield the resource for the range
        that has data."""
        from dango.ingestion.dlt_sources.google_sheets.helpers.data_processing import (
            ParsedRange,
        )

        populated_range = ParsedRange(
            sheet_name="Sheet1", start_col="A", start_row=1, end_col="B", end_row=2
        )
        meta_range = ParsedRange(
            sheet_name="Sheet1", start_col="A", start_row=1, end_col="B", end_row=1
        )
        all_range_data = [
            ("EmptySheet", MagicMock(), MagicMock(), None),
            ("DataSheet", populated_range, meta_range, [["id", "amount"], ["1", "100"]]),
        ]
        meta_values = {
            "sheets": [
                {
                    "properties": {"title": "Sheet1"},
                    "data": [
                        {
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "formattedValue": "id",
                                            "effectiveValue": {"stringValue": "id"},
                                        },
                                        {
                                            "formattedValue": "amount",
                                            "effectiveValue": {"stringValue": "amount"},
                                        },
                                    ]
                                },
                                {
                                    "values": [
                                        {"formattedValue": "1"},
                                        {"formattedValue": "100"},
                                    ]
                                },
                            ]
                        }
                    ],
                }
            ]
        }

        from dango.ingestion.dlt_sources.google_sheets import google_spreadsheet

        with (
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_auth",
                return_value=MagicMock(),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_known_range_names",
                return_value=(["Sheet1"], [], "Test Spreadsheet"),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_data_for_ranges",
                return_value=all_range_data,
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_meta_for_ranges",
                return_value=meta_values,
            ),
        ):
            consumed_rows = list(
                google_spreadsheet(
                    spreadsheet_url_or_id="test-id",
                    range_names=["EmptySheet", "DataSheet"],
                )
            )

        # The non-empty range's own data row was loaded (process_range actually
        # ran and produced the {header: value} dict for DataSheet's one data row).
        assert {"id": "1", "amount": "100"} in consumed_rows

        rows = self._spreadsheet_info_rows(consumed_rows)
        skipped_by_name = {row["range_name"]: row["skipped"] for row in rows}
        assert skipped_by_name["EmptySheet"] is True
        assert skipped_by_name["DataSheet"] is False


@pytest.mark.unit
class TestGoogleSheetsThroughRunSource:
    """Regression coverage for the PR #423 incident (a range accidentally
    cleared, silently preserving stale data with zero signal): confirm the
    extractor's own change doesn't regress end-to-end sync behavior.

    Unlike TestGoogleSheetsEmptyRange, these tests go through the real
    `DltPipelineRunner.run_source()` path with mocked Google Sheets API calls,
    a real DuckDB file, and a real dlt pipeline extract/normalize/load — the
    actual empty-replace protection lives one layer up in dlt_runner.py, so a
    mock of the extractor function in isolation can't confirm it still fires.
    """

    def _runner(self, tmp_path):
        from dango.ingestion.dlt_runner import DltPipelineRunner

        return DltPipelineRunner(tmp_path)

    def _source_config(self, empty_sync_policy=None):
        from dango.config.models import DataSource, GoogleSheetsSourceConfig, SourceType

        kwargs = {
            "name": "sheets_regression",
            "type": SourceType.GOOGLE_SHEETS,
            "google_sheets": GoogleSheetsSourceConfig(
                spreadsheet_url_or_id="test-id", range_names=["DataSheet"]
            ),
        }
        if empty_sync_policy is not None:
            kwargs["empty_sync_policy"] = empty_sync_policy
        return DataSource(**kwargs)

    @staticmethod
    def _populated_range_data():
        from dango.ingestion.dlt_sources.google_sheets.helpers.data_processing import (
            ParsedRange,
        )

        parsed_range = ParsedRange(
            sheet_name="Sheet1", start_col="A", start_row=1, end_col="B", end_row=2
        )
        meta_values = {
            "sheets": [
                {
                    "properties": {"title": "Sheet1"},
                    "data": [
                        {
                            "rowData": [
                                {
                                    "values": [
                                        {
                                            "formattedValue": "id",
                                            "effectiveValue": {"stringValue": "id"},
                                        },
                                        {
                                            "formattedValue": "amount",
                                            "effectiveValue": {"stringValue": "amount"},
                                        },
                                    ]
                                },
                                {
                                    "values": [
                                        {"formattedValue": "1"},
                                        {"formattedValue": "100"},
                                    ]
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        range_data = [("DataSheet", parsed_range, parsed_range, [["id", "amount"], ["1", "100"]])]
        return range_data, meta_values

    @staticmethod
    def _empty_range_data():
        # A real ParsedRange (not MagicMock) — this scenario runs through the
        # real dlt pipeline serializer, which chokes on MagicMock's
        # auto-generated ._asdict() (infinite mock nesting -> recursion
        # error) when it tries to serialize the spreadsheet_info metadata row.
        from dango.ingestion.dlt_sources.google_sheets.helpers.data_processing import (
            ParsedRange,
        )

        parsed_range = ParsedRange(
            sheet_name="Sheet1", start_col="A", start_row=1, end_col="B", end_row=1
        )
        return [("DataSheet", parsed_range, parsed_range, None)]

    def _sync_with_range_data(self, runner, source_config, range_data, meta_values=None):
        patches = [
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_auth",
                return_value=MagicMock(),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_known_range_names",
                return_value=(["Sheet1"], [], "Test Spreadsheet"),
            ),
            patch(
                "dango.ingestion.dlt_sources.google_sheets.api_calls.get_data_for_ranges",
                return_value=range_data,
            ),
        ]
        if meta_values is not None:
            patches.append(
                patch(
                    "dango.ingestion.dlt_sources.google_sheets.api_calls.get_meta_for_ranges",
                    return_value=meta_values,
                )
            )
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return runner.run_source(source_config)

    def test_allow_policy_completes_successfully_with_empty_range(self, tmp_path):
        """Acceptance criterion: a source with empty_sync_policy="allow" and an
        empty range completes as a normal success (not a crash)."""
        runner = self._runner(tmp_path)
        source_config = self._source_config(empty_sync_policy="allow")

        populated, meta_values = self._populated_range_data()
        self._sync_with_range_data(runner, source_config, populated, meta_values)

        result = self._sync_with_range_data(runner, source_config, self._empty_range_data())

        assert result["status"] == "success"
        assert "RuntimeError" not in str(result.get("error", ""))

    def test_default_block_policy_no_longer_raises_raw_runtime_error(self, tmp_path):
        """The extractor itself must never raise RuntimeError again — this
        confirms PR #423's original hard-crash mode is gone even for the
        default ("block") policy.

        KNOWN GAP (out of this task's scope — requires a dlt_runner.py change,
        which this task is explicitly not allowed to touch; flagged for
        coordinating-chat follow-up): with empty_sync_policy left at the
        default "block", this sync currently reports status == "success", not
        "failed". dlt_runner's empty-replace protection
        (DltPipelineRunner._run_dlt_source) has two mechanisms, and neither
        fires here:
          - Source-level 0-row check: never triggers for google_sheets
            because the source always yields a non-empty `spreadsheet_info`
            resource (one metadata row per configured range, skipped or not)
            — so `rows_loaded` is never actually 0 at the source level.
          - Per-table truncation check: requires the affected table's
            post-sync row count to be 0. Because a skipped range is now
            omitted entirely (via `continue`) rather than emitted as an empty
            `write_disposition="replace"` resource, dlt never touches that
            table this run — its post-sync count equals its pre-sync count,
            not 0, so the truncation check never sees the drop it's looking
            for. (`_detect_write_disposition` also returns False once every
            configured range is empty, since no "replace" resource exists in
            the source's `resources` for the check to find.)
        Net effect: no data is lost (the table is simply left untouched,
        confirmed below), but empty_sync_policy="block" does not currently
        stop the sync or signal failure the way the acceptance criteria in
        this task's spec describe — it silently leaves the table stale beyond
        the log warning, which re-creates the "no signal" failure mode PR
        #423 was written to fix, just relocated from a hard crash to a quiet
        success. Confirmed via a live run_source() call below.
        """
        runner = self._runner(tmp_path)
        source_config = self._source_config()  # empty_sync_policy left unset -> "block"
        assert source_config.empty_sync_policy == "block"

        populated, meta_values = self._populated_range_data()
        first_result = self._sync_with_range_data(runner, source_config, populated, meta_values)
        assert first_result["status"] == "success"

        second_result = self._sync_with_range_data(runner, source_config, self._empty_range_data())

        # No raw RuntimeError/traceback — the crash PR #423 introduced is gone.
        assert "RuntimeError" not in str(second_result.get("error", ""))

        # Existing data was not lost (the table was left untouched this run).
        import duckdb

        con = duckdb.connect(str(runner.duckdb_path), read_only=True)
        try:
            rows = con.execute("select id, amount from raw_sheets_regression.data_sheet").fetchall()
        finally:
            con.close()
        assert rows == [("1", "100")]

        # Document current (gap) behavior precisely rather than assert the
        # not-yet-true "should block" outcome — see KNOWN GAP note above.
        assert second_result["status"] == "success"
