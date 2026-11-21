"""tests/unit/test_connector_key_facebook.py

Primary-key contract for the vendored Facebook Ads connector: the Facebook SDK and Graph API objects are replaced with
`export_all_data()` payloads in the shape the API returns (ids as strings) and load through real dlt.
"""

from __future__ import annotations

import importlib
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.factories.connector_contract import KeyCase, assert_key_contract
from tests.factories.sdk_fakes import fake_sdk

FB_PKG = "dango.ingestion.dlt_sources.facebook_ads"


def _fb_sdk():
    return fake_sdk(
        {
            "facebook_business": {"FacebookAdsApi": object},
            "facebook_business.adobjects.abstractobject": {},
            "facebook_business.adobjects.abstractcrudobject": {},
            "facebook_business.adobjects.ad": {},
            "facebook_business.adobjects.adaccount": {},
            "facebook_business.adobjects.adcreative": {},
            "facebook_business.adobjects.adset": {},
            "facebook_business.adobjects.campaign": {},
            "facebook_business.adobjects.lead": {},
            "facebook_business.adobjects.user": {},
            "facebook_business.api": {},
        },
        FB_PKG,
    )


RESOURCES = ("campaigns", "ads", "ad_sets", "ad_creatives", "facebook_insights")


def _obj(data: dict) -> SimpleNamespace:
    return SimpleNamespace(export_all_data=lambda: dict(data))


class _FakeAccount:
    def __init__(self, rev: int, ids: tuple[str, ...]):
        self.rev, self.ids = rev, ids

    def _entities(self, **_kw):
        return [
            _obj({"id": i, "name": f"n{'x' if self.rev else ''}-{i}", "status": "ACTIVE"})
            for i in self.ids
        ]

    get_campaigns = get_ads = get_ad_sets = get_ad_creatives = _entities

    def get_insights(self, params, is_async):
        return params


def _insight_rows(params: dict, rev: int, ids: tuple[str, ...]) -> list[SimpleNamespace]:
    day = params["time_ranges"][0]["since"]
    return [
        _obj(
            {
                "campaign_id": "10",
                "adset_id": "20",
                "ad_id": i,
                "date_start": day,
                "date_stop": day,
                "impressions": "200" if rev else "100",
                "clicks": "5",
                "actions": [{"action_type": "link_click", "value": "3"}],
            }
        )
        for i in ids
    ]


def _case(ids: tuple[str, ...]) -> KeyCase:
    @contextmanager
    def open_(rev):
        account = _FakeAccount(rev, ids)

        def job(params):
            return SimpleNamespace(get_result=lambda: _insight_rows(params, rev, ids))

        with _fb_sdk():
            fb_module = importlib.import_module(FB_PKG)
            fb_module.get_ads_account = lambda *a, **k: account
            fb_module.execute_job = job
            source = fb_module.facebook_ads_combined(
                account_id="act_1",
                access_token="tok",
                initial_load_past_days=1,
                attribution_window_days_lag=0,
            )
            yield source.with_resources(*RESOURCES)

    return KeyCase(
        id=f"facebook_ads[{len(ids)} ads]",
        open=open_,
        pk=dict.fromkeys(("campaigns", "ads", "ad_sets", "ad_creatives"), ("id",))
        | {"facebook_insights": ("campaign_id", "adset_id", "ad_id", "date_start")},
        probe=("ads", "id", "name", "nx-31"),
    )


@pytest.mark.unit
class TestFacebookAdsKeyContract:
    def test_entities_and_daily_insights_keys_exist_and_dedup(self, tmp_path):
        state = assert_key_contract(_case(("31", "32")), tmp_path)
        assert state["facebook_insights"].rows >= 2

    def test_string_ids_are_stored_as_bigint_key_columns(self, tmp_path):
        state = assert_key_contract(_case(("31",)), tmp_path)
        assert {"campaign_id", "adset_id", "ad_id", "date_start"} <= set(
            state["facebook_insights"].columns
        )
