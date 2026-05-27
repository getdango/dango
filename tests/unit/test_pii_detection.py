"""tests/unit/test_pii_detection.py

Unit tests for PII detection (dango/governance/pii_detector.py).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dango.governance.pii_detector import (
    ENTITY_MIN_MATCH_RATIO,
    _cache_findings,
    _get_analyzer,
    _get_existing_keys,
    _is_string_type,
    _register_intl_phone_recognizer,
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

    @pytest.fixture(autouse=True)
    def _reset_analyzer_globals(self) -> None:
        """Reset module-level globals before each test."""
        import dango.governance.pii_detector as pii_module

        pii_module._analyzer = None
        pii_module._analyzer_init_failed = False
        yield
        pii_module._analyzer = None
        pii_module._analyzer_init_failed = False

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
        """On OSError, downloads .whl via urllib and pip installs it."""
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
                patch("subprocess.check_call") as mock_pip,
                patch("presidio_analyzer.AnalyzerEngine", return_value=mock_analyzer),
                patch(
                    "presidio_analyzer.nlp_engine.NlpEngineProvider",
                    return_value=mock_provider,
                ),
            ):
                result = _get_analyzer()
                # spacy.cli.download is NOT called (removed: it calls sys.exit)
                mock_spacy.cli.download.assert_not_called()
                # urllib + pip install fallback used instead
                mock_pip.assert_called_once()
                assert result is mock_analyzer
        finally:
            del sys.modules["spacy"]

    def test_returns_none_on_download_failure(self) -> None:
        """Returns None when urllib/pip download also fails."""
        import sys

        mock_spacy = MagicMock()
        mock_spacy.load.side_effect = OSError("Model not found")
        sys.modules["spacy"] = mock_spacy
        try:
            with (
                patch(f"{_PII}._analyzer", None),
                patch("subprocess.check_call", side_effect=RuntimeError("pip failed")),
            ):
                result = _get_analyzer()
                assert result is None
        finally:
            del sys.modules["spacy"]

    def test_returns_none_on_analyzer_engine_failure(self) -> None:
        import sys

        mock_spacy = MagicMock()
        sys.modules["spacy"] = mock_spacy
        try:
            with (
                patch(f"{_PII}._analyzer", None),
                patch(
                    "presidio_analyzer.AnalyzerEngine",
                    side_effect=RuntimeError("engine init failed"),
                ),
                patch("presidio_analyzer.nlp_engine.NlpEngineProvider"),
            ):
                result = _get_analyzer()
                assert result is None
        finally:
            del sys.modules["spacy"]


@pytest.mark.unit
class TestScanColumn:
    """Unit tests for _scan_column."""

    def test_returns_empty_dict_when_analyzer_is_none(self) -> None:
        with patch(f"{_PII}._get_analyzer", return_value=None):
            result = _scan_column(["test@example.com"])
        assert result == {}

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


@pytest.mark.unit
class TestEntityMinMatchRatio:
    """Tests for PERSON match ratio filtering in _scan_column."""

    def test_person_below_threshold_filtered(self) -> None:
        """PERSON detected in <30% of values should be filtered out."""
        # 1 PERSON match out of 10 values = 10% < 30%
        r = MagicMock(entity_type="PERSON", score=0.85)
        mock_analyzer = MagicMock()
        # Only the first value triggers PERSON, rest return nothing
        mock_analyzer.analyze.side_effect = [[r]] + [[] for _ in range(9)]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column([f"val{i}" for i in range(10)], total_values=10)
        assert "PERSON" not in result

    def test_person_above_threshold_kept(self) -> None:
        """PERSON detected in >=30% of values should be kept."""
        r = MagicMock(entity_type="PERSON", score=0.85)
        mock_analyzer = MagicMock()
        # 4 PERSON matches out of 10 values = 40% >= 30%
        mock_analyzer.analyze.side_effect = [[r]] * 4 + [[] for _ in range(6)]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column([f"val{i}" for i in range(10)], total_values=10)
        assert "PERSON" in result

    def test_person_at_exact_threshold_kept(self) -> None:
        """PERSON detected in exactly 30% of values should be kept (>=, not >)."""
        r = MagicMock(entity_type="PERSON", score=0.85)
        mock_analyzer = MagicMock()
        # 3 PERSON matches out of 10 values = 30% == threshold
        mock_analyzer.analyze.side_effect = [[r]] * 3 + [[] for _ in range(7)]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column([f"val{i}" for i in range(10)], total_values=10)
        assert "PERSON" in result

    def test_non_person_entities_no_minimum(self) -> None:
        """Non-PERSON entities should pass through even with 1 match."""
        r = MagicMock(entity_type="EMAIL_ADDRESS", score=0.85)
        mock_analyzer = MagicMock()
        mock_analyzer.analyze.side_effect = [[r]] + [[] for _ in range(9)]
        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column([f"val{i}" for i in range(10)], total_values=10)
        assert "EMAIL_ADDRESS" in result

    def test_entity_min_match_ratio_has_person(self) -> None:
        assert "PERSON" in ENTITY_MIN_MATCH_RATIO
        assert ENTITY_MIN_MATCH_RATIO["PERSON"] == 0.30


@pytest.mark.unit
class TestIntlPhoneRecognizer:
    """Tests for _register_intl_phone_recognizer."""

    def test_registers_recognizer(self) -> None:
        mock_analyzer = MagicMock()
        mock_registry = MagicMock()
        mock_analyzer.registry = mock_registry
        _register_intl_phone_recognizer(mock_analyzer)
        mock_registry.add_recognizer.assert_called_once()
        recognizer = mock_registry.add_recognizer.call_args[0][0]
        assert recognizer.supported_entities == ["PHONE_NUMBER"]


@pytest.mark.unit
class TestOverrideApplication:
    """Tests for override application in scan_table_for_pii."""

    def test_not_pii_override_removes_finding(self, tmp_path: Path) -> None:
        """Columns marked 'not_pii' should be excluded from findings."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.side_effect = [
            [("email", "VARCHAR")],
            [("test@example.com",)],
        ]
        overrides = {"email": "not_pii"}
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(
                f"{_PII}._scan_column",
                return_value={"EMAIL_ADDRESS": {"confidence": 0.9, "count": 1}},
            ),
            patch(f"{_PII}._cache_findings"),
            patch(
                "dango.governance.pii_overrides.get_overrides_for_table",
                return_value=overrides,
            ),
        ):
            result = scan_table_for_pii(tmp_path, "shopify", "orders")
        assert len(result) == 0

    def test_pii_override_adds_manual_finding(self, tmp_path: Path) -> None:
        """Columns marked 'pii' but not auto-detected should appear as MANUAL_OVERRIDE."""
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.side_effect = [
            [("country", "VARCHAR")],
            [("USA",)],
        ]
        overrides = {"country": "pii"}
        with (
            patch("duckdb.connect", return_value=mock_conn),
            patch(f"{_PII}._scan_column", return_value={}),
            patch(f"{_PII}._cache_findings"),
            patch(
                "dango.governance.pii_overrides.get_overrides_for_table",
                return_value=overrides,
            ),
        ):
            result = scan_table_for_pii(tmp_path, "shopify", "orders")
        assert len(result) == 1
        assert result[0]["entity_type"] == "MANUAL_OVERRIDE"
        assert result[0]["confidence"] == 1.0

    def test_no_overrides_passes_through(self, tmp_path: Path) -> None:
        """No overrides should not change findings."""
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
            patch(f"{_PII}._cache_findings"),
            patch(
                "dango.governance.pii_overrides.get_overrides_for_table",
                return_value={},
            ),
        ):
            result = scan_table_for_pii(tmp_path, "shopify", "orders")
        assert len(result) == 1
        assert result[0]["entity_type"] == "EMAIL_ADDRESS"


@pytest.mark.unit
class TestStructuredDataHeuristic:
    """Tests for BUG-185: structured data PERSON suppression."""

    def test_person_suppressed_for_long_structured_values(self) -> None:
        """PGN-like strings with delimiters suppress PERSON detections."""
        pgn = (
            '[Event "Rated Blitz game"] [Site "https://lichess.org/abc123"] '
            '[White "Magnus_Carlsen"] [Black "Hikaru_Nakamura"] '
            "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 {Spanish Opening} 4. Ba4 Nf6 "
            "5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O"
        )
        values = [pgn] * 5  # avg_len > 100, contains brackets
        mock_analyzer = MagicMock()

        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        mock_result.score = 0.85
        mock_analyzer.analyze.return_value = [mock_result]

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(values)

        assert "PERSON" not in result

    def test_person_kept_for_short_values(self) -> None:
        """Short strings with delimiters still keep PERSON."""
        values = ["[John Smith]"] * 5  # avg_len < 100
        mock_analyzer = MagicMock()
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        mock_result.score = 0.85
        mock_analyzer.analyze.return_value = [mock_result]

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(values)

        assert "PERSON" in result

    def test_person_kept_for_long_values_without_delimiters(self) -> None:
        """Long plain text without structured delimiters keeps PERSON."""
        long_text = "John Smith is a person who " + "does things " * 20
        values = [long_text] * 5  # avg_len > 100, no delimiters
        mock_analyzer = MagicMock()
        mock_result = MagicMock()
        mock_result.entity_type = "PERSON"
        mock_result.score = 0.85
        mock_analyzer.analyze.return_value = [mock_result]

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(values)

        assert "PERSON" in result

    def test_non_person_not_affected(self) -> None:
        """EMAIL_ADDRESS is not suppressed by the structured data heuristic."""
        pgn = (
            '[Event "Rated Blitz game"] [Site "https://lichess.org/abc123"] '
            '[White "Magnus_Carlsen"] [Black "Hikaru_Nakamura"] '
            "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 4. Ba4 Nf6 "
            "5. O-O Be7 6. Re1 b5 7. Bb3 d6 8. c3 O-O"
        )
        values = [pgn] * 5
        mock_analyzer = MagicMock()
        mock_result = MagicMock()
        mock_result.entity_type = "EMAIL_ADDRESS"
        mock_result.score = 0.9
        mock_analyzer.analyze.return_value = [mock_result]

        with patch(f"{_PII}._get_analyzer", return_value=mock_analyzer):
            result = _scan_column(values)

        assert "EMAIL_ADDRESS" in result
