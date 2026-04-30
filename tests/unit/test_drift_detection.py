"""tests/unit/test_drift_detection.py

Unit tests for schema drift detection (dango/governance/schema_drift.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.governance.schema_drift import (
    _send_drift_webhook,
    accept_drift,
    detect_drift_for_sources,
    detect_table_drift,
    get_drift_history,
    get_sources_needing_attention,
)

_DRIFT = "dango.governance.schema_drift"


def _make_warehouse(tmp_path: Path) -> Path:
    """Create an empty warehouse file so existence checks pass."""
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)
    db_path.touch()
    return db_path


def _mock_httpx_client(status_code: int = 200) -> tuple[MagicMock, MagicMock]:
    """Create mock httpx.Client and response."""
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    return mock_client, mock_response


@pytest.mark.unit
class TestDetectTableDrift:
    """Unit tests for detect_table_drift."""

    def test_first_sync_creates_baseline(self, tmp_path: Path) -> None:
        """First sync stores baseline silently and returns empty list."""
        schema = {"id": "INTEGER", "name": "VARCHAR"}
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value=schema),
            patch(f"{_DRIFT}._get_baseline", return_value=None),
            patch(f"{_DRIFT}._save_baseline") as mock_save,
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert result == []
        mock_save.assert_called_once()

    def test_no_drift_returns_empty(self, tmp_path: Path) -> None:
        """No drift when schema matches baseline exactly."""
        schema = {"id": "INTEGER", "name": "VARCHAR"}
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value=schema),
            patch(f"{_DRIFT}._get_baseline", return_value=schema),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")
        assert result == []

    def test_column_added(self, tmp_path: Path) -> None:
        """Detects a newly added column."""
        with (
            patch(
                f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER", "email": "VARCHAR"}
            ),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._record_drift_events"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "column_added"
        assert result[0]["column_name"] == "email"
        assert "VARCHAR" in result[0]["detail"]

    def test_column_removed(self, tmp_path: Path) -> None:
        """Detects a removed column."""
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER", "name": "VARCHAR"}),
            patch(f"{_DRIFT}._record_drift_events"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "column_removed"
        assert result[0]["column_name"] == "name"

    def test_type_changed(self, tmp_path: Path) -> None:
        """Detects a column type change."""
        with (
            patch(
                f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER", "total": "DOUBLE"}
            ),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER", "total": "INTEGER"}),
            patch(f"{_DRIFT}._record_drift_events"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "type_changed"
        assert result[0]["column_name"] == "total"
        assert "INTEGER -> DOUBLE" in result[0]["detail"]

    def test_multiple_changes(self, tmp_path: Path) -> None:
        """Detects multiple changes in a single call."""
        current = {"id": "INTEGER", "total": "DOUBLE", "email": "VARCHAR"}
        baseline = {"id": "INTEGER", "name": "VARCHAR", "total": "INTEGER"}
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value=current),
            patch(f"{_DRIFT}._get_baseline", return_value=baseline),
            patch(f"{_DRIFT}._record_drift_events"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        event_types = {e["event_type"] for e in result}
        assert event_types == {"column_added", "column_removed", "type_changed"}
        assert len(result) == 3

    def test_baseline_update_after_drift(self, tmp_path: Path) -> None:
        """_record_drift_events is called with current schema on drift."""
        current = {"id": "INTEGER", "email": "VARCHAR"}
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value=current),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._record_drift_events") as mock_record,
        ):
            detect_table_drift(tmp_path, "shopify", "orders")

        mock_record.assert_called_once()
        assert mock_record.call_args[0][4] == current

    def test_resilient_cache_returns_events_on_write_failure(self, tmp_path: Path) -> None:
        """Events are returned even if SQLite write fails."""
        with (
            patch(
                f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER", "email": "VARCHAR"}
            ),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._record_drift_events", side_effect=OSError("disk full")),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "column_added"


@pytest.mark.unit
class TestDetectDriftForSources:
    """Unit tests for detect_drift_for_sources."""

    def test_missing_warehouse_skips(self, tmp_path: Path) -> None:
        """No warehouse file -> skip silently."""
        assert detect_drift_for_sources(tmp_path, ["shopify"]) == []

    def test_table_discovery_per_source(self, tmp_path: Path) -> None:
        """Tables are discovered from DuckDB for each source."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_DRIFT}.detect_table_drift", return_value=[]) as mock_detect,
            patch(f"{_DRIFT}._send_drift_webhook"),
        ):
            detect_drift_for_sources(tmp_path, ["shopify"])
        mock_detect.assert_called_once_with(tmp_path, "shopify", "orders")

    def test_dlt_table_exclusion(self, tmp_path: Path) -> None:
        """dlt internal tables and spreadsheet tables are excluded via SQL."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_DRIFT}.detect_table_drift", return_value=[]),
            patch(f"{_DRIFT}._send_drift_webhook"),
        ):
            detect_drift_for_sources(tmp_path, ["shopify"])
        sql_call = mock_conn.execute.call_args[0][0]
        assert "_dlt_%" in sql_call
        assert "'spreadsheet', 'spreadsheet_info'" in sql_call

    def test_per_source_error_isolation(self, tmp_path: Path) -> None:
        """Error in one source doesn't stop processing of others."""
        _make_warehouse(tmp_path)
        call_count = 0

        def side_effect(*args: object, **kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            mock_conn = MagicMock()
            if call_count == 1:
                mock_conn.execute.side_effect = RuntimeError("source error")
            else:
                mock_conn.execute.return_value.fetchall.return_value = [("items",)]
            return mock_conn

        with (
            patch("duckdb.connect", side_effect=side_effect),
            patch(f"{_DRIFT}.detect_table_drift", return_value=[]) as mock_detect,
            patch(f"{_DRIFT}._send_drift_webhook"),
        ):
            detect_drift_for_sources(tmp_path, ["bad_source", "good_source"])
        mock_detect.assert_called_once_with(tmp_path, "good_source", "items")

    def test_per_table_error_isolation(self, tmp_path: Path) -> None:
        """Error in one table doesn't stop processing of others."""
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",), ("items",)]
        call_count = 0

        def detect_side_effect(*args: object, **kwargs: object) -> list[dict[str, str]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("table error")
            return []

        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_DRIFT}.detect_table_drift", side_effect=detect_side_effect),
            patch(f"{_DRIFT}._send_drift_webhook"),
        ):
            detect_drift_for_sources(tmp_path, ["shopify"])
        assert call_count == 2


@pytest.mark.unit
class TestGetDriftHistory:
    """Unit tests for get_drift_history."""

    def test_newest_first_ordering(self, tmp_path: Path) -> None:
        """Events are returned newest first (descending id)."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            (
                2,
                "shopify",
                "orders",
                "email",
                "column_added",
                "additive",
                "type=VARCHAR",
                "2026-01-02",
            ),
            (
                1,
                "shopify",
                "orders",
                "name",
                "column_removed",
                "breaking",
                "was=VARCHAR",
                "2026-01-01",
            ),
        ]
        with patch(f"{_DRIFT}.connect") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_drift_history(tmp_path)
        assert result[0]["id"] == 2
        assert result[1]["id"] == 1

    def test_source_filter(self, tmp_path: Path) -> None:
        """Source filter is included in the query."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_DRIFT}.connect") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            get_drift_history(tmp_path, source="shopify")
        assert "source = ?" in mock_conn.execute.call_args[0][0]

    def test_table_filter(self, tmp_path: Path) -> None:
        """Table filter is included in the query."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_DRIFT}.connect") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            get_drift_history(tmp_path, table_name="orders")
        assert "table_name = ?" in mock_conn.execute.call_args[0][0]

    def test_limit_applied(self, tmp_path: Path) -> None:
        """Limit parameter is passed in the query."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_DRIFT}.connect") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            get_drift_history(tmp_path, limit=25)
        assert 25 in mock_conn.execute.call_args[0][1]

    def test_empty_result(self, tmp_path: Path) -> None:
        """Empty result returns empty list."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_DRIFT}.connect") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            result = get_drift_history(tmp_path)
        assert result == []


_WH = "dango.platform.notifications.webhook"


@pytest.mark.unit
class TestSendDriftWebhook:
    """Unit tests for _send_drift_webhook."""

    _events: list[dict[str, str]] = [{"event_type": "column_added", "column_name": "x"}]

    def test_no_config_returns_silently(self, tmp_path: Path) -> None:
        """No notification config -> silent return."""
        with patch(f"{_WH}.load_notification_config", return_value=None):
            _send_drift_webhook(tmp_path, ["shopify"], self._events)

    def test_on_governance_false_skips(self, tmp_path: Path) -> None:
        """on_governance=False -> notification is skipped."""
        mock_config = MagicMock()
        mock_config.webhooks = [MagicMock(name="t", url="https://e.com", format="generic")]
        with (
            patch(f"{_WH}.load_notification_config", return_value=mock_config),
            patch(f"{_WH}.should_notify", return_value=False),
        ):
            _send_drift_webhook(tmp_path, ["shopify"], self._events)

    def test_successful_send(self, tmp_path: Path) -> None:
        """Webhook is sent successfully with mock httpx."""
        mock_config = MagicMock()
        wh = MagicMock()
        wh.name, wh.url, wh.format = "test-wh", "https://example.com/hook", "generic"
        mock_config.webhooks = [wh]
        mock_client, _ = _mock_httpx_client()
        with (
            patch(f"{_WH}.load_notification_config", return_value=mock_config),
            patch(f"{_WH}.should_notify", return_value=True),
            patch("httpx.Client", return_value=mock_client),
        ):
            _send_drift_webhook(tmp_path, ["shopify"], self._events)
        mock_client.post.assert_called_once()

    def test_never_raises_on_failure(self, tmp_path: Path) -> None:
        """Webhook send failure does not raise."""
        with patch(f"{_WH}.load_notification_config", side_effect=RuntimeError("err")):
            _send_drift_webhook(tmp_path, ["shopify"], self._events)

    def test_slack_format_dispatch(self, tmp_path: Path) -> None:
        """Slack-formatted webhook calls format_slack_message."""
        mock_config = MagicMock()
        wh = MagicMock()
        wh.name, wh.url, wh.format = "slack-hook", "https://hooks.slack.com/t", "slack"
        mock_config.webhooks = [wh]
        mock_client, _ = _mock_httpx_client()
        with (
            patch(f"{_WH}.load_notification_config", return_value=mock_config),
            patch(f"{_WH}.should_notify", return_value=True),
            patch("httpx.Client", return_value=mock_client),
            patch(
                "dango.platform.notifications.slack.format_slack_message",
                return_value={"attachments": []},
            ) as mock_fmt,
        ):
            _send_drift_webhook(tmp_path, ["shopify"], self._events)
        mock_fmt.assert_called_once()


@pytest.mark.unit
class TestDriftSeverity:
    """Tests for drift severity classification."""

    def test_breaking_drift_severity_column_removed(self, tmp_path: Path) -> None:
        """column_removed event has severity 'breaking'."""
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER"}),
            patch(
                f"{_DRIFT}._get_baseline",
                return_value={"id": "INTEGER", "name": "VARCHAR"},
            ),
            patch(f"{_DRIFT}._record_drift_events_only"),
            patch(f"{_DRIFT}._set_source_attention"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "column_removed"
        assert result[0]["severity"] == "breaking"

    def test_breaking_drift_severity_type_changed(self, tmp_path: Path) -> None:
        """type_changed event has severity 'breaking'."""
        with (
            patch(
                f"{_DRIFT}._get_current_schema",
                return_value={"id": "INTEGER", "total": "DOUBLE"},
            ),
            patch(
                f"{_DRIFT}._get_baseline",
                return_value={"id": "INTEGER", "total": "INTEGER"},
            ),
            patch(f"{_DRIFT}._record_drift_events_only"),
            patch(f"{_DRIFT}._set_source_attention"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "type_changed"
        assert result[0]["severity"] == "breaking"

    def test_additive_drift_severity(self, tmp_path: Path) -> None:
        """column_added event has severity 'additive'."""
        with (
            patch(
                f"{_DRIFT}._get_current_schema",
                return_value={"id": "INTEGER", "email": "VARCHAR"},
            ),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._record_drift_events"),
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        assert len(result) == 1
        assert result[0]["event_type"] == "column_added"
        assert result[0]["severity"] == "additive"

    def test_breaking_drift_skips_baseline_update(self, tmp_path: Path) -> None:
        """Breaking drift records events but does NOT update baseline."""
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER"}),
            patch(
                f"{_DRIFT}._get_baseline",
                return_value={"id": "INTEGER", "name": "VARCHAR"},
            ),
            patch(f"{_DRIFT}._record_drift_events_only") as mock_record_only,
            patch(f"{_DRIFT}._record_drift_events") as mock_record,
            patch(f"{_DRIFT}._set_source_attention") as mock_attention,
        ):
            detect_table_drift(tmp_path, "shopify", "orders")

        # Breaking: uses _record_drift_events_only, NOT _record_drift_events
        mock_record_only.assert_called_once()
        mock_record.assert_not_called()
        mock_attention.assert_called_once()

    def test_additive_drift_updates_baseline(self, tmp_path: Path) -> None:
        """Additive-only drift updates baseline normally."""
        current = {"id": "INTEGER", "email": "VARCHAR"}
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value=current),
            patch(f"{_DRIFT}._get_baseline", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._record_drift_events_only") as mock_record_only,
            patch(f"{_DRIFT}._record_drift_events") as mock_record,
            patch(f"{_DRIFT}._set_source_attention") as mock_attention,
        ):
            detect_table_drift(tmp_path, "shopify", "orders")

        # Additive: uses _record_drift_events (with baseline update)
        mock_record.assert_called_once()
        mock_record_only.assert_not_called()
        mock_attention.assert_not_called()

    def test_mixed_breaking_and_additive_uses_breaking_path(self, tmp_path: Path) -> None:
        """Mixed breaking+additive drift takes the breaking path for all events."""
        # column removed (breaking) + column added (additive) in same diff
        current = {"id": "INTEGER", "email": "VARCHAR"}
        baseline = {"id": "INTEGER", "name": "VARCHAR"}
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value=current),
            patch(f"{_DRIFT}._get_baseline", return_value=baseline),
            patch(f"{_DRIFT}._record_drift_events_only") as mock_record_only,
            patch(f"{_DRIFT}._record_drift_events") as mock_record,
            patch(f"{_DRIFT}._set_source_attention") as mock_attention,
        ):
            result = detect_table_drift(tmp_path, "shopify", "orders")

        # Both events returned
        event_types = {e["event_type"] for e in result}
        assert event_types == {"column_added", "column_removed"}

        # Breaking path used (no baseline update), even though additive events exist
        mock_record_only.assert_called_once()
        mock_record.assert_not_called()
        mock_attention.assert_called_once()

        # All events passed to _record_drift_events_only
        recorded = mock_record_only.call_args[0][1]
        assert len(recorded) == 2


@pytest.mark.unit
class TestSourceAttention:
    """Tests for source attention (breaking drift state management)."""

    def test_source_attention_set_on_breaking(self, tmp_path: Path) -> None:
        """Source attention row is set when breaking drift is detected."""
        with (
            patch(f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER"}),
            patch(
                f"{_DRIFT}._get_baseline",
                return_value={"id": "INTEGER", "name": "VARCHAR"},
            ),
            patch(f"{_DRIFT}._record_drift_events_only"),
            patch(f"{_DRIFT}._set_source_attention") as mock_attention,
        ):
            detect_table_drift(tmp_path, "shopify", "orders")

        mock_attention.assert_called_once()
        call_args = mock_attention.call_args
        assert call_args[0][1] == "shopify"
        events = call_args[0][2]
        assert len(events) == 1
        assert events[0]["severity"] == "breaking"

    def test_accept_drift_clears_attention(self, tmp_path: Path) -> None:
        """accept_drift() clears attention and updates baseline."""
        _make_warehouse(tmp_path)
        with (
            patch(f"{_DRIFT}.connect") as mock_ctx,
            patch(f"{_DRIFT}._get_current_schema", return_value={"id": "INTEGER"}),
            patch(f"{_DRIFT}._save_baseline") as mock_save,
        ):
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

            accept_drift(tmp_path, "shopify")

        # Baseline updated for each table
        mock_save.assert_called_once()
        # Attention cleared (DELETE query was executed)
        delete_calls = [
            c for c in mock_conn.execute.call_args_list if "DELETE FROM source_attention" in str(c)
        ]
        assert len(delete_calls) == 1

    def test_get_sources_needing_attention(self, tmp_path: Path) -> None:
        """get_sources_needing_attention returns correct sources."""
        import json

        mock_events = [{"event_type": "column_removed", "severity": "breaking"}]

        with patch(f"{_DRIFT}.connect") as mock_ctx:
            mock_conn = MagicMock()
            mock_conn.execute.return_value.fetchall.return_value = [
                ("shopify", "1 breaking change(s)", json.dumps(mock_events), "2026-01-01"),
            ]
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)

            result = get_sources_needing_attention(tmp_path)

        assert len(result) == 1
        assert result[0]["source"] == "shopify"
        assert result[0]["reason"] == "1 breaking change(s)"
        assert len(result[0]["drift_events"]) == 1
