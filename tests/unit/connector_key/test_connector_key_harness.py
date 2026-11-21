"""tests/unit/connector_key/test_connector_key_harness.py

The key-contract harness itself: it pins each table's write disposition, rejects an update check that cannot
fail, and checks that an append table gains exactly the new row (instead of skipping the update check).
"""

from __future__ import annotations

from contextlib import contextmanager

import dlt
import pytest

from tests.factories.connector_contract import KeyCase, assert_key_contract


def _toy(write_disposition: str, *, honour_rev: bool = True):
    """A one-table connector: rows 1 and 2; at rev 1 row 3 appears, and (not for append) row 1 changes."""

    @contextmanager
    def open_(rev: int):
        rev = rev if honour_rev else 0
        changes = rev and write_disposition != "append"

        @dlt.resource(name="items", primary_key="id", write_disposition=write_disposition)
        def items(ts=dlt.sources.incremental("ts", initial_value=0)):
            rows = [
                {"id": 1, "ts": 110 if changes else 10, "v": "new" if changes else "old"},
                {"id": 2, "ts": 20, "v": "x"},
            ]
            if rev:
                rows.append({"id": 3, "ts": 300, "v": "added"})
            yield rows

        yield items

    return open_


def _case(write_disposition="merge", **overrides):
    fields = {
        "id": "toy",
        "open": _toy(write_disposition),
        "pk": {"items": ("id",)},
        "disposition": {"items": write_disposition},
    }
    return KeyCase(**{**fields, **overrides})


@pytest.mark.unit
class TestHarnessPinsAndProves:
    def test_a_merge_table_with_a_real_update_passes(self, tmp_path):
        case = _case(probes=(("items", "id", "v", "new"),))
        assert_key_contract(case, tmp_path)

    def test_a_table_that_changed_disposition_fails(self, tmp_path):
        with pytest.raises(AssertionError, match="disposition"):
            assert_key_contract(_case(disposition={"items": "append"}), tmp_path)

    def test_every_keyed_table_needs_a_pinned_disposition(self, tmp_path):
        with pytest.raises(AssertionError, match="disposition"):
            assert_key_contract(_case(disposition={}), tmp_path)

    def test_an_update_check_whose_value_is_already_there_is_rejected(self, tmp_path):
        with pytest.raises(AssertionError, match="already"):
            assert_key_contract(_case(probes=(("items", "id", "v", "old"),)), tmp_path)

    def test_an_update_that_never_happens_fails(self, tmp_path):
        case = _case(open=_toy("merge", honour_rev=False), probes=(("items", "id", "v", "new"),))
        with pytest.raises(AssertionError, match="not updated"):
            assert_key_contract(case, tmp_path)

    def test_an_append_table_gains_exactly_the_new_row(self, tmp_path):
        case = _case("append", append_probes=(("items", "id", 3),))
        assert_key_contract(case, tmp_path)

    def test_an_append_table_that_gains_nothing_fails(self, tmp_path):
        case = _case(
            "append", open=_toy("append", honour_rev=False), append_probes=(("items", "id", 3),)
        )
        with pytest.raises(AssertionError, match="new row"):
            assert_key_contract(case, tmp_path)
