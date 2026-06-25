"""dango/governance/pii_detector.py

PII detection engine using Presidio and spaCy.  Called after each sync via
the post-sync hook dispatcher, with results cached in ``pii_findings``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dango.logging import get_logger
from dango.utils.dango_db import connect
from dango.validation import validate_identifier, validate_source_name

logger = get_logger(__name__)

SCAN_ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "US_SSN",
    "IP_ADDRESS",
    "IBAN_CODE",
    "PERSON",
]

SCORE_THRESHOLD = 0.5
DEFAULT_SAMPLE_SIZE = 100

# Minimum fraction of sampled values that must match before flagging.
# PERSON set high (0.30) to reduce false positives from spaCy NER on
# structured data (chess notation, UUIDs, codes).
ENTITY_MIN_MATCH_RATIO: dict[str, float] = {
    "PERSON": 0.30,
}

_STRING_TYPES = frozenset({"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR"})

# BUG-185: Structured data heuristic constants
_STRUCTURED_DATA_MIN_AVG_LEN = 100
_STRUCTURED_DATA_DELIMITERS = frozenset({"[", "]", "{", "\n"})

_analyzer: Any = None
_analyzer_init_failed: bool = False

_SPACY_MODEL = "en_core_web_sm"
_SPACY_MODEL_URL = (
    "https://github.com/explosion/spacy-models/releases/download/"
    "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
)


def _register_intl_phone_recognizer(analyzer: Any) -> None:
    """Register a pattern-based international phone number recognizer.

    The built-in Presidio phone recognizer is US-centric.  This adds two
    patterns that catch international numbers with country codes (``+1-555-…``,
    ``+44 20 7946…``, ``+61234567890``).
    """
    from presidio_analyzer import Pattern, PatternRecognizer

    intl_phone = PatternRecognizer(
        supported_entity="PHONE_NUMBER",
        name="InternationalPhoneRecognizer",
        patterns=[
            Pattern(
                name="intl_phone_separators",
                regex=r"\+\d{1,3}[\s\-.]?\(?\d{1,4}\)?[\s\-.]?\d{1,4}[\s\-.]?\d{1,9}",
                score=0.7,
            ),
            Pattern(
                name="intl_phone_e164",
                regex=r"\+\d{7,15}",
                score=0.6,
            ),
        ],
    )
    analyzer.registry.add_recognizer(intl_phone)


def _get_analyzer() -> Any | None:
    """Return a cached Presidio ``AnalyzerEngine``, downloading spaCy model if needed.

    Returns ``None`` if the spaCy model cannot be loaded or downloaded,
    allowing callers to gracefully skip PII scanning.
    """
    global _analyzer, _analyzer_init_failed  # noqa: PLW0603
    if _analyzer is not None:
        return _analyzer
    if _analyzer_init_failed:
        return None

    import spacy  # lazy import

    try:
        spacy.load(_SPACY_MODEL)
    except OSError:
        # Download .whl to temp file then pip install locally.
        # spacy.cli.download() is NOT used — it calls sys.exit() internally,
        # which raises SystemExit (BaseException) and kills the process.
        import os
        import subprocess
        import sys
        import tempfile
        import urllib.request

        try:
            whl_name = _SPACY_MODEL_URL.rsplit("/", 1)[-1]
            whl_path = os.path.join(tempfile.gettempdir(), whl_name)
            try:
                urllib.request.urlretrieve(_SPACY_MODEL_URL, whl_path)
                subprocess.check_call(
                    [sys.executable, "-m", "pip", "install", whl_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            finally:
                try:
                    os.unlink(whl_path)
                except OSError:
                    pass
        except Exception:
            _analyzer_init_failed = True
            logger.warning(
                "pii_spacy_download_failed",
                model=_SPACY_MODEL,
                hint="Install manually: python -m spacy download en_core_web_sm",
            )
            return None

    # Suppress verbose Presidio + spaCy loggers during init (use alias to
    # avoid shadowing structlog).  Temporarily raise root logger to ERROR so
    # that AnalyzerEngine() construction doesn't emit startup noise, then
    # restore the original level.
    import logging as _logging

    _noisy_loggers = [
        "presidio-analyzer",
        "presidio_analyzer",
        "presidio_analyzer.nlp_engine",
        "presidio_analyzer.recognizer_registry",
        "presidio_analyzer.pattern_recognizer",
        "spacy",
    ]
    saved_levels: dict[str, int] = {}
    for _name in _noisy_loggers:
        _lg = _logging.getLogger(_name)
        saved_levels[_name] = _lg.level
        _lg.setLevel(_logging.ERROR)

    root_logger = _logging.getLogger()
    saved_root_level = root_logger.level
    root_logger.setLevel(_logging.ERROR)

    try:
        from presidio_analyzer import AnalyzerEngine  # lazy import
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "en", "model_name": _SPACY_MODEL}],
                "ner_model_configuration": {
                    "labels_to_ignore": ["CARDINAL", "PRODUCT", "MONEY"],
                },
            }
        )
        _analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
        _register_intl_phone_recognizer(_analyzer)
    except Exception:
        _analyzer_init_failed = True
        logger.warning("pii_analyzer_init_failed", exc_info=True)
        return None
    finally:
        root_logger.setLevel(saved_root_level)
        for _name, _lvl in saved_levels.items():
            _logging.getLogger(_name).setLevel(_lvl)
    return _analyzer


def scan_sources_for_pii(
    project_root: Path,
    sources: list[str],
) -> list[dict[str, Any]]:
    """Scan freshly synced sources for PII.

    Discovers tables per source, runs PII analysis, and sends webhook
    notifications only for newly detected findings.

    Args:
        project_root: Path to the Dango project root.
        sources: Names of sources that synced successfully.

    Returns:
        Flat list of all finding dicts across all sources and tables.
    """
    import duckdb  # lazy import

    db_path = project_root / "data" / "warehouse.duckdb"
    if not db_path.exists():
        logger.debug("pii_skip_no_warehouse", path=str(db_path))
        return []

    all_findings: list[dict[str, Any]] = []

    for source in sources:
        try:
            validate_source_name(source)
            logger.debug("pii_source_start", source=source)

            # Get existing keys BEFORE scanning for first-detection logic
            existing_keys = _get_existing_keys(project_root, source)

            schema = f"raw_{source}"
            conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
            try:
                tables = conn.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = ? "
                    "AND table_name NOT LIKE '_dlt_%' "
                    "ORDER BY table_name",
                    [schema],
                ).fetchall()
            finally:
                conn.close()

            source_findings: list[dict[str, Any]] = []
            for (tbl_name,) in tables:
                try:
                    tbl_name = validate_identifier(tbl_name)
                    findings = scan_table_for_pii(project_root, source, tbl_name)
                    source_findings.extend(findings)
                except Exception:
                    logger.warning(
                        "pii_table_error",
                        source=source,
                        table=tbl_name,
                    )

            all_findings.extend(source_findings)

            # Send webhook only for truly new findings
            new_findings = [
                f
                for f in source_findings
                if (f["source"], f["table_name"], f["column_name"], f["entity_type"])
                not in existing_keys
            ]
            if new_findings:
                _send_pii_webhook(project_root, sources, new_findings)

            logger.debug("pii_source_complete", source=source)
        except Exception:
            logger.warning("pii_source_error", source=source)

    return all_findings


def scan_table_for_pii(
    project_root: Path,
    source: str,
    table_name: str,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> list[dict[str, Any]]:
    """Scan a single table for PII by sampling string columns.

    Args:
        project_root: Path to the Dango project root.
        source: Source name (used as ``raw_{source}`` schema).
        table_name: Table name within the source schema.
        sample_size: Maximum number of distinct values to sample per column.

    Returns:
        List of finding dicts (source, table_name, column_name, entity_type,
        confidence, sample_count, scanned_at).
    """
    import duckdb  # lazy import

    source = validate_source_name(source)
    table_name = validate_identifier(table_name)

    db_path = project_root / "data" / "warehouse.duckdb"
    schema = f"raw_{source}"

    conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
    try:
        columns = conn.execute(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? "
            "ORDER BY ordinal_position",
            [schema, table_name],
        ).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc).isoformat()
    findings: list[dict[str, Any]] = []

    for col_name, data_type in columns:
        # Skip internal dlt metadata columns — they never contain user PII.
        # Case-insensitive: DuckDB may return uppercase identifiers.
        if col_name.lower().startswith("_dlt_"):
            continue
        if not _is_string_type(data_type):
            continue

        try:
            col_name = validate_identifier(col_name)

            conn = duckdb.connect(str(db_path), config={"access_mode": "read_only"})
            try:
                values = conn.execute(
                    f'SELECT DISTINCT "{col_name}"::VARCHAR '
                    f'FROM "{schema}"."{table_name}" '
                    f'WHERE "{col_name}" IS NOT NULL LIMIT ?',
                    [sample_size],
                ).fetchall()
            finally:
                conn.close()

            str_values = [row[0] for row in values if row[0]]
            if not str_values:
                continue

            detected = _scan_column(str_values, total_values=len(str_values))
            for entity_type, info in detected.items():
                findings.append(
                    {
                        "source": source,
                        "table_name": table_name,
                        "column_name": col_name,
                        "entity_type": entity_type,
                        "confidence": info["confidence"],
                        "sample_count": info["count"],
                        "scanned_at": now,
                    }
                )
        except Exception:
            logger.warning(
                "pii_column_error",
                source=source,
                table=table_name,
                column=col_name,
            )

    # Apply PII overrides (dismiss false positives / add manual marks)
    from dango.governance.pii_overrides import get_overrides_for_table

    overrides = get_overrides_for_table(project_root, source, table_name)
    if overrides:
        # Remove findings for columns marked 'not_pii'
        findings = [f for f in findings if overrides.get(f["column_name"]) != "not_pii"]
        # Add synthetic findings for columns marked 'pii' not auto-detected
        detected_cols = {f["column_name"] for f in findings}
        for col_name, status in overrides.items():
            if status == "pii" and col_name not in detected_cols:
                findings.append(
                    {
                        "source": source,
                        "table_name": table_name,
                        "column_name": col_name,
                        "entity_type": "MANUAL_OVERRIDE",
                        "confidence": 1.0,
                        "sample_count": 0,
                        "scanned_at": now,
                    }
                )

    # Resilient cache pattern: compute → try cache → return regardless
    try:
        _cache_findings(project_root, source, table_name, findings)
    except Exception:
        logger.warning(
            "pii_cache_error",
            source=source,
            table=table_name,
        )

    return findings


def get_pii_findings(
    project_root: Path,
    *,
    source: str | None = None,
    table_name: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query cached PII findings, newest first.

    Args:
        project_root: Path to the Dango project root.
        source: Optional source name filter.
        table_name: Optional table name filter.
        limit: Maximum number of findings to return.

    Returns:
        List of finding dicts, newest first.
    """
    conditions: list[str] = []
    params: list[str | int] = []

    if source is not None:
        source = validate_source_name(source)
        conditions.append("source = ?")
        params.append(source)

    if table_name is not None:
        table_name = validate_identifier(table_name)
        conditions.append("table_name = ?")
        params.append(table_name)

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    params.append(limit)

    query = (
        "SELECT id, source, table_name, column_name, entity_type, "
        "confidence, sample_count, scanned_at "
        f"FROM pii_findings {where_clause} "
        "ORDER BY id DESC LIMIT ?"
    )

    with connect(project_root) as conn:
        rows = conn.execute(query, params).fetchall()

    return [
        {
            "id": row[0],
            "source": row[1],
            "table_name": row[2],
            "column_name": row[3],
            "entity_type": row[4],
            "confidence": row[5],
            "sample_count": row[6],
            "scanned_at": row[7],
        }
        for row in rows
    ]


def _is_string_type(data_type: str) -> bool:
    """Check whether a DuckDB data type is a string type."""
    return data_type.upper() in _STRING_TYPES


def _scan_column(values: list[str], total_values: int = 0) -> dict[str, dict[str, Any]]:
    """Run Presidio analyzer on sampled values, aggregating by entity type.

    Args:
        values: Sampled string values from the column.
        total_values: Total number of sampled values (used for match-ratio
            filtering).  Defaults to ``len(values)`` when 0.
    """
    analyzer = _get_analyzer()
    if analyzer is None:
        return {}
    if total_values <= 0:
        total_values = len(values)
    detections: dict[str, dict[str, Any]] = {}

    for val in values:
        results = analyzer.analyze(
            text=val,
            entities=SCAN_ENTITIES,
            language="en",
        )
        for result in results:
            if result.score < SCORE_THRESHOLD:
                continue
            entity = result.entity_type
            if entity not in detections:
                detections[entity] = {"confidence": result.score, "count": 1}
            else:
                detections[entity]["count"] += 1
                if result.score > detections[entity]["confidence"]:
                    detections[entity]["confidence"] = result.score

    # BUG-185: Suppress PERSON on structured data columns
    if "PERSON" in detections and values:
        avg_len = sum(len(v) for v in values) / len(values)
        if avg_len > _STRUCTURED_DATA_MIN_AVG_LEN:
            has_delimiters = any(any(d in v for d in _STRUCTURED_DATA_DELIMITERS) for v in values)
            if has_delimiters:
                del detections["PERSON"]

    # Filter entities that don't meet the minimum match ratio
    if total_values > 0:
        filtered: dict[str, dict[str, Any]] = {}
        for entity, info in detections.items():
            min_ratio = ENTITY_MIN_MATCH_RATIO.get(entity)
            if min_ratio is not None and info["count"] / total_values < min_ratio:
                continue
            filtered[entity] = info
        return filtered

    return detections


def _cache_findings(
    project_root: Path,
    source: str,
    table_name: str,
    findings: list[dict[str, Any]],
) -> None:
    """Cache PII findings in SQLite (DELETE + INSERT for same source/table)."""
    with connect(project_root) as conn:
        conn.execute(
            "DELETE FROM pii_findings WHERE source = ? AND table_name = ?",
            (source, table_name),
        )
        for f in findings:
            conn.execute(
                "INSERT INTO pii_findings "
                "(source, table_name, column_name, entity_type, confidence, "
                "sample_count, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f["source"],
                    f["table_name"],
                    f["column_name"],
                    f["entity_type"],
                    f["confidence"],
                    f["sample_count"],
                    f["scanned_at"],
                ),
            )
        conn.commit()


def _get_existing_keys(
    project_root: Path,
    source: str,
) -> set[tuple[str, str, str, str]]:
    """Return existing (source, table, column, entity_type) tuples from cache."""
    try:
        with connect(project_root) as conn:
            rows = conn.execute(
                "SELECT source, table_name, column_name, entity_type "
                "FROM pii_findings WHERE source = ?",
                (source,),
            ).fetchall()
        return {(row[0], row[1], row[2], row[3]) for row in rows}
    except Exception:
        logger.warning("pii_existing_keys_error", source=source)
        return set()


def _send_pii_webhook(
    project_root: Path,
    sources: list[str],
    findings: list[dict[str, Any]],
) -> None:
    """Send webhook notification for detected PII findings.  Never raises."""
    try:
        from dango.platform.notifications.webhook import (
            EventType,
            WebhookPayload,
            load_notification_config,
            should_notify,
        )

        config = load_notification_config(project_root)
        if config is None:
            return

        if not should_notify(EventType.PII_DETECTED, config):
            return

        if not config.webhooks:
            return

        # Build summary string
        entity_counts: dict[str, int] = {}
        for f in findings:
            entity_counts[f["entity_type"]] = entity_counts.get(f["entity_type"], 0) + 1
        summary_parts = [f"{count} {etype}" for etype, count in entity_counts.items()]
        summary = f"PII detected: {', '.join(summary_parts)}"

        payload = WebhookPayload(
            event_type=EventType.PII_DETECTED,
            schedule_name="post_sync",
            sources=sources,
            error=summary,
            occurred_at=datetime.now(tz=timezone.utc),
        )

        import httpx  # lazy import

        for webhook in config.webhooks:
            try:
                if webhook.format == "slack":
                    from dango.platform.notifications.slack import format_slack_message

                    json_payload: dict[str, Any] = format_slack_message(payload)
                else:
                    json_payload = {
                        "event": payload.event_type.value,
                        "schedule": payload.schedule_name,
                        "sources": payload.sources,
                        "error": payload.error,
                        "timestamp": (
                            payload.occurred_at.isoformat() if payload.occurred_at else None
                        ),
                    }

                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(webhook.url, json=json_payload)

                logger.info(
                    "pii_webhook_delivered",
                    webhook=webhook.name,
                    status=resp.status_code,
                )
            except Exception:
                logger.warning(
                    "pii_webhook_error",
                    webhook=webhook.name,
                    exc_info=True,
                )
    except Exception:
        logger.warning("pii_webhook_unexpected_error", exc_info=True)
