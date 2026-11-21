"""tests/unit/connector_key/test_connector_key_coverage.py

Coverage register for the primary-key contract: every vendored connector is listed with how it is checked, so a
new connector cannot be added without saying so. Also guards a flag that fails every sync under dlt >= 1.26.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import dango.ingestion.dlt_sources as sources_pkg

SOURCES_DIR = Path(sources_pkg.__file__).parent
TESTS_DIR = Path(__file__).parent

FULL = "covered: synthetic API payloads through real dlt"
NO_KEY = "no primary key declared on any resource"
CONSTANT = "constant vendor object id (`id`/`gid`/`_id`), no fixture built in WP-40a"

# connector -> (status, evidence test file or reason)
COVERAGE: dict[str, tuple[str, str]] = {
    "airtable": (FULL, "test_connector_key_airtable.py"),
    "slack": (FULL, "test_connector_key_slack.py"),
    "github": (
        FULL + " (repo events; reactions and stargazers declare no key)",
        "test_connector_key_http_sources.py",
    ),
    "matomo": (
        FULL + " (visits, visitors; reports declare no key)",
        "test_connector_key_http_sources.py",
    ),
    "freshdesk": (FULL, "test_connector_key_http_sources.py"),
    "google_analytics": (FULL + " (protobuf report types)", "test_connector_key_google.py"),
    "google_ads": (FULL + " (GAQL-shaped rows, client replaced)", "test_connector_key_google.py"),
    "facebook_ads": (
        FULL + " (Graph objects replaced; leads transformer not run)",
        "test_connector_key_facebook.py",
    ),
    "chess": ("not covered", NO_KEY),
    "jira": ("not covered", NO_KEY),
    "mux": ("not covered", NO_KEY),
    "strapi": ("not covered", NO_KEY),
    "salesforce": ("not covered", NO_KEY + "; SDK needs credentials"),
    "kafka": ("not covered", NO_KEY + "; needs a broker"),
    "inbox": ("not covered", "IMAP server needed (message_uid, file_hash keys)"),
    "kinesis": ("not covered", "AWS stream needed; key is a connector-generated column"),
    "mongodb": ("not covered", "pymongo is not an installed dependency; key is `_id`"),
    "asana_dlt": ("not covered", "asana SDK is not an installed dependency; key is `gid`"),
    "google_sheets": (
        "not covered",
        "merge_key `spreadsheet_id` is connector-generated; Sheets client needs credentials",
    ),
    "hubspot": (
        "not covered",
        CONSTANT + "; list keys (id, stage) set by the connector's own pivot",
    ),
    "notion": ("not covered", CONSTANT),
    "personio": ("not covered", CONSTANT + "; composite keys are connector-generated columns"),
    "pipedrive": ("not covered", CONSTANT),
    "shopify_dlt": ("not covered", CONSTANT),
    "stripe_analytics": ("not covered", CONSTANT + "; stripe SDK is not an installed dependency"),
    "workable": ("not covered", CONSTANT),
    "zendesk": ("not covered", CONSTANT),
}


def _connector_dirs() -> set[str]:
    return {p.name for p in SOURCES_DIR.iterdir() if p.is_dir() and not p.name.startswith("__")}


@pytest.mark.unit
class TestKeyCoverageRegister:
    def test_every_vendored_connector_is_registered_and_nothing_stale(self):
        assert _connector_dirs() == set(COVERAGE)

    def test_covered_connectors_point_at_an_existing_test_file(self):
        for name, (status, evidence) in COVERAGE.items():
            if status.startswith("covered"):
                assert (TESTS_DIR / evidence).is_file(), f"{name}: {evidence} does not exist"

    def test_no_connector_requires_an_external_scheduler(self):
        # dlt >= 1.26 raises ExternalSchedulerNotAvailable at extract for such an incremental when no
        # interval is injected (Airflow, DLT_INTERVAL_*), which is every Dango sync.
        offenders = [
            str(path.relative_to(SOURCES_DIR))
            for path in SOURCES_DIR.rglob("*.py")
            if "allow_external_schedulers=True" in path.read_text(encoding="utf-8")
        ]
        assert offenders == []
