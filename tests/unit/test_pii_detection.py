"""tests/unit/test_pii_detection.py

Unit tests for PII detection (dango/governance/pii_detector.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.governance.pii_detector import (
    _cache_findings,
    _get_analyzer,
    _get_existing_keys,
    _is_string_type,
    _scan_column,
    _send_pii_webhook,
    get_pii_findings,
    scan_sources_for_pii,
    scan_table_for_pii,
)

_PII = "dango.governance.pii_detector"
_WH = "dango.platform.notifications.webhook"


def _make_warehouse(tmp_path: Path) -> Path:
    db_path = tmp_path / "data" / "warehouse.duckdb"
    db_path.parent.mkdir(parents=True)
    db_path.touch()
    return db_path


def _mock_httpx_client(status_code: int = 200) -> tuple[MagicMock, MagicMock]:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.return_value = mock_response
    return mock_client, mock_response


def _mock_connect(mock_conn: MagicMock) -> MagicMock:
    """Return a patch target for connect() that yields mock_conn."""
    mock_ctx = MagicMock()
    mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
    return mock_ctx


@pytest.mark.unit
class TestIsStringType:
    """Unit tests for _is_string_type helper."""

    def test_varchar_is_string(self) -> None:
        assert _is_string_type("VARCHAR") is True

    def test_text_is_string(self) -> None:
        assert _is_string_type("TEXT") is True

    def test_integer_is_not_string(self) -> None:
        assert _is_string_type("INTEGER") is False

    def test_case_insensitive(self) -> None:
        assert _is_string_type("varchar") is True
        assert _is_string_type("Varchar") is True


@pytest.mark.unit
class TestGetAnalyzer:
    """Unit tests for _get_analyzer singleton."""

    def test_caches_analyzer(self) -> None:
        import sys

        mock_analyzer = MagicMock()
        mock_provider = MagicMock()
        mock_provider.create_engine.return_value = MagicMock()
        with (
            patch(f"{_PII}._analyzer", None),
            patch(f"{_PII}.spacy", create=True) as mock_spacy,
            patch("presidio_analyzer.AnalyzerEngine", return_value=mock_analyzer) as mock_cls,
            patch("presidio_analyzer.nlp_engine.NlpEngineProvider", return_value=mock_provider),
        ):
            sys.modules["spacy"] = mock_spacy
            try:
                result = _get_analyzer()
                assert result is mock_analyzer
                mock_cls.assert_called_once_with(
                    nlp_engine=mock_provider.create_engine.return_value
                )
            finally:
                del sys.modules["spacy"]

    def test_download_fallback(self) -> None:
        import sys

        mock_spacy = MagicMock()
        mock_spacy.load.side_effect = OSError("Model not found")
        mock_analyzer = MagicMock()
        mock_provider = MagicMock()
        mock_provider.create_engine.return_value = MagicMock()
        sys.modules["spacy"] = mock_spacy
        try:
            with (
                patch(f"{_PII}._analyzer", None),
                patch("presidio_analyzer.AnalyzerEngine", return_value=mock_analyzer),
                patch("presidio_analyzer.nlp_engine.NlpEngineProvider", return_value=mock_provider),
            ):
                result = _get_analyzer()
                mock_spacy.cli.download.assert_called_once_with("en_core_web_sm")
                assert result is mock_analyzer
        finally:
            del sys.modules["spacy"]

    def test_runtime_error_on_download_failure(self) -> None:
        import sys

        mock_spacy = MagicMock()
        mock_spacy.load.side_effect = OSError("Model not found")
        mock_spacy.cli.download.side_effect = RuntimeError("download failed")
        sys.modules["spacy"] = mock_spacy
        try:
            with patch(f"{_PII}._analyzer", None):
                with pytest.raises(RuntimeError, match="spaCy model"):
                    _get_analyzer()
        finally:
            del sys.modules["spacy"]


@pytest.mark.unit
class TestScanColumn:
    """Unit tests for _scan_column."""

    def test_detects_entities(self) -> None:
        r = MagicMock(entity_type="EMAIL_ADDRESS", score=0.85)
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [r]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(["test@example.com"])
        assert "EMAIL_ADDRESS" in result
        assert result["EMAIL_ADDRESS"]["confidence"] == 0.85
        assert result["EMAIL_ADDRESS"]["count"] == 1

    def test_filters_below_threshold(self) -> None:
        r = MagicMock(entity_type="PERSON", score=0.3)
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = [r]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(["John"])
        assert result == {}

    def test_aggregates_multiple_detections(self) -> None:
        r1 = MagicMock(entity_type="EMAIL_ADDRESS", score=0.7)
        r2 = MagicMock(entity_type="EMAIL_ADDRESS", score=0.9)
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.side_effect = [[r1], [r2]]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(["a@b.com", "c@d.com"])
        assert result["EMAIL_ADDRESS"]["confidence"] == 0.9
        assert result["EMAIL_ADDRESS"]["count"] == 2

    def test_empty_values(self) -> None:
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.return_value = []
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column([])
        assert result == {}


@pytest.mark.unit
class TestScanTableForPii:
    """Unit tests for scan_table_for_pii."""

    def test_scans_string_columns_only(self) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.side_effect = [
            [("email", "VARCHAR"), ("age", "INTEGER"), ("name", "TEXT")],
            [("test@example.com",)],
            [("John",)],
        ]
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_PII}._scan_column", return_value={}) as mock_scan,
            patch(f"{_PII}._cache_findings"),
        ):
            scan_table_for_pii(Path("/tmp/test"), "shopify", "orders")
        assert mock_scan.call_count == 2

    def test_resilient_cache_returns_findings(self) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.side_effect = [
            [("email", "VARCHAR")],
            [("test@example.com",)],
        ]
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(
                f"{_PII}._scan_column",
                return_value={"EMAIL_ADDRESS": {"confidence": 0.9, "count": 1}},
            ),
            patch(f"{_PII}._cache_findings", side_effect=OSError("disk full")),
        ):
            result = scan_table_for_pii(Path("/tmp/test"), "shopify", "orders")
        assert len(result) == 1
        assert result[0]["entity_type"] == "EMAIL_ADDRESS"

    def test_per_column_error_isolation(self) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.side_effect = [
            [("email", "VARCHAR"), ("phone", "VARCHAR")],
            RuntimeError("column error"),
            [("555-1234",)],
        ]
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_PII}._scan_column", return_value={}),
            patch(f"{_PII}._cache_findings"),
        ):
            scan_table_for_pii(Path("/tmp/test"), "shopify", "orders")


@pytest.mark.unit
class TestScanSourcesForPii:
    """Unit tests for scan_sources_for_pii."""

    def test_missing_warehouse_skips(self, tmp_path: Path) -> None:
        assert scan_sources_for_pii(tmp_path, ["shopify"]) == []

    def test_table_discovery_per_source(self, tmp_path: Path) -> None:
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_PII}.scan_table_for_pii", return_value=[]) as mock_scan,
            patch(f"{_PII}._get_existing_keys", return_value=set()),
            patch(f"{_PII}._send_pii_webhook"),
        ):
            scan_sources_for_pii(tmp_path, ["shopify"])
        mock_scan.assert_called_once_with(tmp_path, "shopify", "orders")

    def test_per_source_error_isolation(self, tmp_path: Path) -> None:
        _make_warehouse(tmp_path)
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            mc = MagicMock()
            if call_count == 1:
                mc.execute.side_effect = RuntimeError("source error")
            else:
                mc.execute.return_value.fetchall.return_value = [("items",)]
            return mc

        with (
            patch("duckdb.connect", side_effect=side_effect),
            patch(f"{_PII}.scan_table_for_pii", return_value=[]) as mock_scan,
            patch(f"{_PII}._get_existing_keys", return_value=set()),
            patch(f"{_PII}._send_pii_webhook"),
        ):
            scan_sources_for_pii(tmp_path, ["bad_source", "good_source"])
        mock_scan.assert_called_once_with(tmp_path, "good_source", "items")

    def test_webhook_for_new_findings_only(self, tmp_path: Path) -> None:
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
        existing = {("shopify", "orders", "email", "EMAIL_ADDRESS")}
        new_f = {
            "source": "shopify",
            "table_name": "orders",
            "column_name": "phone",
            "entity_type": "PHONE_NUMBER",
            "confidence": 0.8,
            "sample_count": 5,
            "scanned_at": "2026-01-01",
        }
        old_f = {
            "source": "shopify",
            "table_name": "orders",
            "column_name": "email",
            "entity_type": "EMAIL_ADDRESS",
            "confidence": 0.9,
            "sample_count": 10,
            "scanned_at": "2026-01-01",
        }
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_PII}.scan_table_for_pii", return_value=[new_f, old_f]),
            patch(f"{_PII}._get_existing_keys", return_value=existing),
            patch(f"{_PII}._send_pii_webhook") as mock_webhook,
        ):
            scan_sources_for_pii(tmp_path, ["shopify"])
        mock_webhook.assert_called_once()
        sent = mock_webhook.call_args[0][2]
        assert len(sent) == 1
        assert sent[0]["entity_type"] == "PHONE_NUMBER"

    def test_no_webhook_when_all_existing(self, tmp_path: Path) -> None:
        _make_warehouse(tmp_path)
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("orders",)]
        existing = {("shopify", "orders", "email", "EMAIL_ADDRESS")}
        old_f = {
            "source": "shopify",
            "table_name": "orders",
            "column_name": "email",
            "entity_type": "EMAIL_ADDRESS",
            "confidence": 0.9,
            "sample_count": 10,
            "scanned_at": "2026-01-01",
        }
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_PII}.scan_table_for_pii", return_value=[old_f]),
            patch(f"{_PII}._get_existing_keys", return_value=existing),
            patch(f"{_PII}._send_pii_webhook") as mock_webhook,
        ):
            scan_sources_for_pii(tmp_path, ["shopify"])
        mock_webhook.assert_not_called()


@pytest.mark.unit
class TestGetPiiFindings:
    """Unit tests for get_pii_findings."""

    def test_newest_first_ordering(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            (2, "shopify", "orders", "phone", "PHONE_NUMBER", 0.8, 5, "2026-01-02"),
            (1, "shopify", "orders", "email", "EMAIL_ADDRESS", 0.9, 10, "2026-01-01"),
        ]
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            result = get_pii_findings(tmp_path)
        assert result[0]["id"] == 2
        assert result[1]["id"] == 1

    def test_source_filter(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            get_pii_findings(tmp_path, source="shopify")
        assert "source = ?" in mock_conn.execute.call_args[0][0]

    def test_table_filter(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            get_pii_findings(tmp_path, table_name="orders")
        assert "table_name = ?" in mock_conn.execute.call_args[0][0]

    def test_limit_applied(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            get_pii_findings(tmp_path, limit=25)
        assert 25 in mock_conn.execute.call_args[0][1]

    def test_empty_result(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            result = get_pii_findings(tmp_path)
        assert result == []


@pytest.mark.unit
class TestSendPiiWebhook:
    """Unit tests for _send_pii_webhook."""

    _findings: list[dict[str, str]] = [{"entity_type": "EMAIL_ADDRESS", "column_name": "email"}]

    def test_no_config_returns_silently(self, tmp_path: Path) -> None:
        with patch(f"{_WH}.load_notification_config", return_value=None):
            _send_pii_webhook(tmp_path, ["shopify"], self._findings)

    def test_on_governance_false_skips(self, tmp_path: Path) -> None:
        mock_config = MagicMock()
        mock_config.webhooks = [MagicMock(name="t", url="https://e.com", format="generic")]
        with (
            patch(f"{_WH}.load_notification_config", return_value=mock_config),
            patch(f"{_WH}.should_notify", return_value=False),
        ):
            _send_pii_webhook(tmp_path, ["shopify"], self._findings)

    def test_successful_send(self, tmp_path: Path) -> None:
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
            _send_pii_webhook(tmp_path, ["shopify"], self._findings)
        mock_client.post.assert_called_once()

    def test_never_raises_on_failure(self, tmp_path: Path) -> None:
        with patch(f"{_WH}.load_notification_config", side_effect=RuntimeError("err")):
            _send_pii_webhook(tmp_path, ["shopify"], self._findings)

    def test_slack_format_dispatch(self, tmp_path: Path) -> None:
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
            _send_pii_webhook(tmp_path, ["shopify"], self._findings)
        mock_fmt.assert_called_once()


@pytest.mark.unit
class TestCacheFindings:
    """Unit tests for _cache_findings."""

    def test_deletes_before_insert(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            findings = [
                {
                    "source": "shopify",
                    "table_name": "orders",
                    "column_name": "email",
                    "entity_type": "EMAIL_ADDRESS",
                    "confidence": 0.9,
                    "sample_count": 10,
                    "scanned_at": "2026-01-01",
                }
            ]
            _cache_findings(tmp_path, "shopify", "orders", findings)
        first_sql = mock_conn.execute.call_args_list[0][0][0]
        assert "DELETE FROM pii_findings" in first_sql
        second_sql = mock_conn.execute.call_args_list[1][0][0]
        assert "INSERT INTO pii_findings" in second_sql


@pytest.mark.unit
class TestGetExistingKeys:
    """Unit tests for _get_existing_keys."""

    def test_returns_tuples(self, tmp_path: Path) -> None:
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [
            ("shopify", "orders", "email", "EMAIL_ADDRESS"),
        ]
        with patch(f"{_PII}.connect", _mock_connect(mock_conn)):
            result = _get_existing_keys(tmp_path, "shopify")
        assert result == {("shopify", "orders", "email", "EMAIL_ADDRESS")}

    def test_returns_empty_on_error(self, tmp_path: Path) -> None:
        with patch(f"{_PII}.connect", side_effect=OSError("db error")):
            result = _get_existing_keys(tmp_path, "shopify")
        assert result == set()
