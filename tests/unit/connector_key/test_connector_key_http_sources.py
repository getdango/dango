"""tests/unit/connector_key/test_connector_key_http_sources.py

Primary-key contract for the vendored GitHub events, Matomo visits and Freshdesk connectors: synthetic API
replies (stubbed at the HTTP transport) loaded through real dlt for awkward response shapes.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import pytest

from dango.ingestion.dlt_sources.freshdesk import freshdesk_source
from dango.ingestion.dlt_sources.github import github_repo_events
from dango.ingestion.dlt_sources.matomo import matomo_visits
from tests.factories.connector_contract import FakeHttp, KeyCase, assert_key_contract

NOW = int(time.time())
OLD = NOW - 7 * 86400


def _qs(request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(request.url).query).items()}


# -- GitHub repository events ----------------------------------------------------------------------------------


def _event(event_id, event_type, created_at="2024-05-01T10:00:00Z", **extra):
    return {
        "id": event_id,
        "type": event_type,
        "created_at": created_at,
        "actor": {"id": 7, "login": "ann"},
        "payload": {"commits": [{"sha": "a1", "message": "m"}], "ref": "refs/heads/main"},
        **extra,
    }


def _github_case(events, case_id, pk, new_event):
    """`new_event` appears at rev 1 (a later created_at); its type's table must gain exactly that row."""
    table = {"PushEvent": "push_event", "IssuesEvent": "issues_event"}[new_event["type"]]

    @contextmanager
    def open_(rev):
        served = [*events, new_event] if rev else events
        http = FakeHttp().route(
            r"api\.github\.com/repos/o/n/events", (200, served, {"x-ratelimit-remaining": "59"})
        )
        with http.active():
            yield github_repo_events(owner="o", name="n", access_token="ghp_fake")

    return KeyCase(
        id=case_id,
        open=open_,
        pk=pk,
        disposition=dict.fromkeys(pk, "append"),
        append_probes=((table, "id", new_event["id"]),),
    )


@pytest.mark.unit
class TestGithubEventsKeyContract:
    def test_event_types_become_tables_keyed_on_event_id(self, tmp_path):
        events = [
            _event("40000000001", "PushEvent"),
            _event("40000000002", "WatchEvent", created_at="2024-05-01T11:00:00Z"),
            _event("40000000003", "PushEvent", created_at="2024-05-01T12:00:00Z"),
        ]
        case = _github_case(
            events,
            "github[events]",
            {"push_event": ("id",), "watch_event": ("id",)},
            _event("40000000009", "PushEvent", created_at="2024-05-02T10:00:00Z"),
        )
        state = assert_key_contract(case, tmp_path)
        assert state["push_event"].rows == 2

    def test_numeric_event_ids(self, tmp_path):
        events = [
            _event(3000000000001, "IssuesEvent"),
            _event(3000000000002, "IssuesEvent", created_at="2024-05-01T11:00:00Z"),
        ]
        case = _github_case(
            events,
            "github[numeric-id]",
            {"issues_event": ("id",)},
            _event(3000000000009, "IssuesEvent", created_at="2024-05-02T10:00:00Z"),
        )
        assert_key_contract(case, tmp_path)

    def test_events_that_share_a_timestamp_are_all_kept(self, tmp_path):
        events = [_event(str(i), "PushEvent") for i in (101, 102, 103)]
        case = _github_case(
            events,
            "github[same-created-at]",
            {"push_event": ("id",)},
            _event("109", "PushEvent", created_at="2024-05-02T10:00:00Z"),
        )
        assert (
            assert_key_contract(case, tmp_path)["push_event"].rows == 3
        )  # all three kept at rev 0


# -- Matomo visits and visitors --------------------------------------------------------------------------------


def _visit(visit_id, visitor_id, server_ts):
    return {
        "idVisit": visit_id,
        "visitorId": visitor_id,
        "serverTimestamp": server_ts,
        "firstActionTimestamp": server_ts - 30,
        "lastActionTimestamp": server_ts,
        "actionDetails": [{"type": "action", "url": "https://x.invalid/"}],
        "referrerType": "direct",
    }


def _matomo_case(with_visitors: bool):
    def bulk(request, rev):
        # rev 1: visitor vis0 has made one more visit (id 4), so only that visit is new and its profile changes
        visits = [_visit(i, f"vis{i % 2}", OLD + i) for i in ((1, 2, 3, 4) if rev else (1, 2, 3))]
        params = _qs(request)
        urls = params.get("urls[0]", "")
        if "Live.getLastVisitsDetails" in urls:
            offset = int(parse_qs(urls)["filter_offset"][0])
            return [visits if offset == 0 else []]
        profiles = []
        for key, value in params.items():
            if key.startswith("urls["):
                profiles.append(
                    {"visitorId": parse_qs(value)["visitorId"][0], "totalVisits": 3 if rev else 2}
                )
        return profiles

    @contextmanager
    def open_(rev):
        with FakeHttp().route(r"matomo\.invalid/index\.php", lambda r: bulk(r, rev)).active():
            yield matomo_visits(
                api_token="tok",
                url="https://matomo.invalid",
                live_events_site_id=1,
                get_live_event_visitors=with_visitors,
            )

    pk = {"visits": ("id_visit",)}
    if with_visitors:
        pk["visitors"] = ("visitor_id",)
    return KeyCase(
        id=f"matomo[visitors={with_visitors}]",
        open=open_,
        pk=pk,
        disposition={"visits": "append", **({"visitors": "merge"} if with_visitors else {})},
        probes=(("visitors", "visitor_id", "total_visits", 3),) if with_visitors else (),
        append_probes=(("visits", "id_visit", 4),),
    )


@pytest.mark.unit
class TestMatomoKeyContract:
    def test_visits_keyed_on_camel_case_id_visit(self, tmp_path):
        state = assert_key_contract(_matomo_case(False), tmp_path)
        assert state["visits"].rows == 3

    def test_visitors_transformer_keyed_on_visitor_id(self, tmp_path):
        state = assert_key_contract(_matomo_case(True), tmp_path)
        assert state["visitors"].rows >= 1


# -- Freshdesk -------------------------------------------------------------------------------------------------


def _freshdesk_case(endpoints, records_by_endpoint, case_id, pk, probes):
    def handler_for(endpoint, rev):
        def handler(request):
            return records_by_endpoint(endpoint, rev) if _qs(request).get("page") == "1" else []

        return handler

    def open_(rev):
        @contextmanager
        def ctx():
            http = FakeHttp()
            for endpoint in endpoints:
                http.route(rf"fd\.freshdesk\.com/api/v2/{endpoint}\?", handler_for(endpoint, rev))
            with http.active():
                yield freshdesk_source(endpoints=endpoints, domain="fd", api_secret_key="key")

        return ctx()

    return KeyCase(
        id=case_id, open=open_, pk=pk, disposition=dict.fromkeys(pk, "merge"), probes=probes
    )


@pytest.mark.unit
class TestFreshdeskKeyContract:
    def test_default_style_endpoints_keyed_on_id(self, tmp_path):
        def records(endpoint, rev):
            # a real update moves updated_at forward, which is what the incremental cursor sees
            status, stamp = (3, "2024-05-03T10:00:00Z") if rev else (2, "2024-05-02T10:00:00Z")
            return [
                {
                    "id": 1,
                    "name": "A",
                    "status": status,
                    "updated_at": stamp,
                    "custom_fields": {"x": None},
                },
                {
                    "id": 2,
                    "name": "B",
                    "status": 2,
                    "updated_at": "2024-05-01T10:00:00Z",
                    "tags": ["a", "b"],
                },
            ]

        case = _freshdesk_case(
            ["tickets", "agents"],
            records,
            "freshdesk[tickets+agents]",
            {"tickets": ("id",), "agents": ("id",)},
            probes=(("tickets", "id", "status", 3),),
        )
        assert_key_contract(case, tmp_path)
