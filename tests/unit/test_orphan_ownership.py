"""tests/unit/test_orphan_ownership.py

Which warehouse tables are orphaned, decided from planted schemas (no sync needed): shared schemas,
schemas that only a sync state links to a configured source, tables of configured endpoints, schemas
a source claims under a different spelling. `dango db clean` never drops anything ambiguous.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.factories.custom_dataset_projects import (
    orphan_schemas,
    orphans,
    plant,
    sources_of,
    tables_by_schema,
)
from tests.factories.dlt_state_projects import db_clean, entry, isolate_home, project

pytestmark = pytest.mark.unit

STATE = "pipeline_name"


@pytest.fixture(autouse=True)
def _throwaway_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_home(monkeypatch, tmp_path / "home")


def config_entry(name: str, type_: str, **block: object) -> dict[str, object]:
    return {"name": name, "type": type_, type_: block}


def test_a_schema_claimed_in_a_different_case_is_configured(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="RAW_Custom"))
    plant(root, "raw_custom", ["events", "_dlt_loads"])
    assert orphans(root) == []


def test_a_schema_no_source_claims_is_orphaned(tmp_path: Path) -> None:
    """Control: default-named source."""
    root = project(tmp_path, "p1", *sources_of(b=None))
    plant(root, "raw_b")
    plant(root, "raw_ghost", ["t"], state="ghost")
    assert orphan_schemas(root) == {"raw_ghost"}


def test_a_dataset_equal_to_another_sources_default_is_claimed_and_survives_the_others_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(a=None, b="raw_a"))
    plant(root, "raw_a", ["events"], state="b")
    plant(root, "raw_b", ["leftover"])
    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "raw_a" in tables_by_schema(root)
    assert "Kept raw_a" in result.output
    assert "more than one source" in result.output

    from tests.factories.dlt_state_projects import write_sources

    write_sources(root, *sources_of(b="raw_a"))
    assert "raw_a" not in orphan_schemas(root)


def test_a_schema_only_a_sync_state_links_to_a_configured_source_is_kept_with_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`b` once loaded into raw_old; its config now says raw_new. Whose data raw_old is: unclear."""
    root = project(tmp_path, "p1", *sources_of(b="raw_new"))
    plant(root, "raw_old", ["events"], state="b")
    plant(root, "raw_ghost", ["t"], state="ghost")

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "raw_old" in tables_by_schema(root)
    assert "raw_ghost" not in tables_by_schema(root)
    kept = [line for line in result.output.splitlines() if "Kept raw_old" in line]
    assert len(kept) == 1
    assert "'b'" in kept[0]
    assert "Dropped raw_ghost.t" in result.output


def test_a_state_naming_a_configured_pipeline_name_override_keeps_the_schema(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, "p1", entry("b", pipeline_name="pb", dataset_name="raw_new"))
    plant(root, "raw_old", ["events"], state="PB")
    assert orphans(root) == []


def test_an_unreadable_state_table_keeps_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", entry("b"))
    plant(root, "raw_odd", ["events"], state_sql="SELECT 1 AS something_else")
    result = db_clean(root, monkeypatch)
    assert result.exit_code == 0, result.output
    assert "raw_odd" in tables_by_schema(root)
    assert "Kept raw_odd" in result.output


def test_a_schema_without_a_state_table_and_without_a_claim_is_orphaned(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", entry("b"))
    plant(root, "raw_odd", ["events"])
    assert orphan_schemas(root) == {"raw_odd"}


def test_the_staging_schema_of_a_claimed_dataset_is_configured(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_custom"))
    plant(root, "raw_custom", ["events"])
    plant(root, "raw_custom_staging", ["events"])
    plant(root, "raw_stale_staging", ["events"])
    assert orphan_schemas(root) == {"raw_stale_staging"}


def test_a_dataset_called_raw_claims_the_raw_schema(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw"))
    plant(root, "raw", ["events"])
    assert orphans(root) == []


def test_the_dbt_schemas_are_never_listed_even_when_a_source_names_one(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="staging", c="marts"))
    plant(root, "staging", ["stg_x__events", "events"])
    plant(root, "marts", ["m"])
    plant(root, "intermediate", ["i"])
    assert orphans(root) == []


def test_the_tables_of_a_configured_endpoint_include_its_child_tables(tmp_path: Path) -> None:
    root = project(
        tmp_path, "p1", config_entry("stripe", "stripe", endpoints=["Customer", "InvoiceItem"])
    )
    plant(root, "raw_stripe", ["customer", "customer__sources", "invoice_item", "_dlt_loads"])
    plant(root, "raw_stripe", ["charge"])
    assert orphans(root) == [("raw_stripe", "charge")]


def test_a_source_without_a_table_list_owns_its_whole_schema(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", config_entry("stripe", "stripe"))
    plant(root, "raw_stripe", ["customer", "anything"])
    assert orphans(root) == []


def test_two_sources_in_one_schema_own_all_of_it_when_one_lists_no_tables(
    tmp_path: Path,
) -> None:
    root = project(
        tmp_path,
        "p1",
        config_entry("stripe", "stripe", endpoints=["customer"]),
        entry("b", dataset_name="raw_stripe"),
    )
    plant(root, "raw_stripe", ["customer", "events"])
    assert orphans(root) == []


def test_a_rest_api_source_with_endpoint_blocks_does_not_stop_the_scan_and_keeps_its_tables(
    tmp_path: Path,
) -> None:
    root = project(
        tmp_path,
        "p1",
        config_entry(
            "api", "rest_api", base_url="https://x.invalid", endpoints=[{"name": "users"}]
        ),
    )
    plant(root, "raw_api", ["users", "users__tags", "other"])
    plant(root, "raw_ghost", ["t"])
    assert orphan_schemas(root) == {"raw_ghost"}


def test_a_file_source_owns_raw_name(tmp_path: Path) -> None:
    """Control: file sources load straight into `raw_<name>` (no dlt rewriting)."""
    root = project(
        tmp_path, "p1", config_entry("sales", "csv", directory="data/uploads", file_pattern="*.csv")
    )
    plant(root, "raw_sales", ["sales"])
    assert orphans(root) == []


def test_the_clean_lists_only_the_corrected_orphans_before_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_custom"))
    plant(root, "raw_custom", ["events"], state="b")
    plant(root, "raw_ghost", ["t"], state="ghost")

    result = db_clean(root, monkeypatch, answer="n\n")

    assert result.exit_code == 0, result.output
    assert "raw_ghost.t" in result.output
    assert "raw_custom" not in result.output
    assert "Found 2 orphaned table(s)" in result.output
    assert set(tables_by_schema(root)) == {"raw_custom", "raw_ghost"}


def test_a_disabled_source_still_claims_its_schema_without_any_sync_state(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, "p1", entry("b", dataset_name="raw_custom") | {"enabled": False})
    plant(root, "raw_custom", ["events"])
    assert orphans(root) == []


def test_a_schema_created_in_mixed_case_is_matched_to_its_claim(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_custom"))
    plant(root, "raw_Custom", ["events"])
    assert orphans(root) == []


def test_the_staging_schema_of_a_kept_schema_is_kept_too(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_new"))
    plant(root, "raw_old", ["events"], state="b")
    plant(root, "raw_old_staging", ["events"])
    assert orphans(root) == []


@pytest.mark.parametrize("dataset", ["rawdata", "raw1"])
def test_a_claimed_dataset_that_only_looks_like_a_raw_schema_is_configured(
    tmp_path: Path, dataset: str
) -> None:
    """`LIKE 'raw_%'` matches `rawdata`; the claim, not the spelling, decides."""
    root = project(tmp_path, "p1", *sources_of(b=dataset))
    plant(root, dataset, ["events", "_dlt_loads"])
    assert orphans(root) == []


def test_a_leftover_table_of_a_configured_source_is_kept_with_the_right_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", config_entry("stripe", "stripe", endpoints=["customer"]))
    plant(root, "raw_stripe", ["customer", "customer__sources", "invoice"], state="stripe")

    result = db_clean(root, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "invoice" in tables_by_schema(root)["raw_stripe"]
    line = next(x for x in result.output.splitlines() if "Kept raw_stripe" in x)
    assert "does not list" in line
    assert "'stripe'" in line
    assert "does not load into" not in line


def test_a_leftover_table_of_a_configured_source_without_a_sync_state_is_dropped_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: no new keep and no new drop where the sync state does not name the source."""
    root = project(tmp_path, "p1", config_entry("stripe", "stripe", endpoints=["customer"]))
    plant(root, "raw_stripe", ["customer", "customer__sources", "invoice"])
    assert orphans(root) == [("raw_stripe", "invoice")]
    db_clean(root, monkeypatch)
    assert tables_by_schema(root)["raw_stripe"] == ["customer", "customer__sources"]


def test_an_unclaimed_schema_is_not_described_as_one_the_source_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_new"))
    plant(root, "raw_old", ["events"], state="b")
    line = next(x for x in db_clean(root, monkeypatch).output.splitlines() if "Kept raw_old" in x)
    assert "does not load into this schema" in line
    assert "does not list" not in line


def test_a_file_source_with_a_trailing_underscore_owns_the_literal_schema(
    tmp_path: Path,
) -> None:
    """`sales_` is written to `raw_sales_` (not rewritten by dlt): it has no state table either."""
    root = project(
        tmp_path,
        "p1",
        config_entry("sales_", "csv", directory="data/uploads", file_pattern="*.csv"),
    )
    plant(root, "raw_sales_", ["sales_"])
    assert orphans(root) == []


def test_a_dataset_ending_in_staging_is_claimed_without_any_sync_state(tmp_path: Path) -> None:
    root = project(tmp_path, "p1", *sources_of(b="raw_x_staging"))
    plant(root, "raw_x_staging", ["events"])
    assert orphans(root) == []
