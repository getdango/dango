"""tests/unit/connector_key/test_connector_key_fakes_restore.py

The SDK and pyairtable stand-ins must leave no trace: after their context exits, the connector package attribute
on `dango.ingestion.dlt_sources` is what it was before, so `from ... import google_ads` never returns a fake-bound copy.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.factories.airtable_fake import airtable_source, airtable_table
from tests.factories.sdk_fakes import FACEBOOK_MODULES, fake_sdk

PARENT = "dango.ingestion.dlt_sources"
MISSING = object()


def _parent():
    return importlib.import_module(PARENT)


def _attr(name: str):
    return _parent().__dict__.get(name, MISSING)


@pytest.mark.unit
class TestFakesLeaveNoTrace:
    def test_sdk_fake_removes_the_package_attribute_it_created(self, monkeypatch):
        monkeypatch.delitem(sys.modules, f"{PARENT}.facebook_ads", raising=False)
        monkeypatch.delattr(_parent(), "facebook_ads", raising=False)
        with fake_sdk(FACEBOOK_MODULES, f"{PARENT}.facebook_ads"):
            fake_bound = importlib.import_module(f"{PARENT}.facebook_ads")
            assert _attr("facebook_ads") is fake_bound
        assert _attr("facebook_ads") is MISSING
        assert f"{PARENT}.facebook_ads" not in sys.modules

    def test_sdk_fake_restores_the_attribute_that_was_there_before(self, monkeypatch):
        earlier = object()
        monkeypatch.setattr(_parent(), "facebook_ads", earlier, raising=False)
        with fake_sdk(FACEBOOK_MODULES, f"{PARENT}.facebook_ads"):
            importlib.import_module(f"{PARENT}.facebook_ads")
        assert _attr("facebook_ads") is earlier

    def test_airtable_fake_removes_the_package_attribute_it_created(self, monkeypatch):
        monkeypatch.delitem(sys.modules, f"{PARENT}.airtable", raising=False)
        monkeypatch.delattr(_parent(), "airtable", raising=False)
        with airtable_source([airtable_table("T", "Name")], {}):
            assert _attr("airtable") is not MISSING
        assert _attr("airtable") is MISSING

    def test_airtable_fake_restores_the_attribute_that_was_there_before(self, monkeypatch):
        earlier = object()
        monkeypatch.setattr(_parent(), "airtable", earlier, raising=False)
        with airtable_source([airtable_table("T", "Name")], {}):
            pass
        assert _attr("airtable") is earlier
