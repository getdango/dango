"""tests/unit/test_model_service.py

Tests for the non-interactive dbt model service (naming, lookup, validation, render, create/update/remove).
"""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from dango.transformation import model_service as ms
from dango.transformation.generator import DbtModelGenerator
from dango.transformation.model_service import (
    ModelServiceError,
    create_model,
    extract_refs,
    find_model,
    normalize_model_name,
    render_model_sql,
    update_model,
    validate_model_sql,
)

pytestmark = pytest.mark.unit


def _write(root: Path, rel: str, content: str = "select 1") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    _write(tmp_path, "dbt/models/staging/stg_orders__orders.sql", "select 1 as id, 2 as amount")
    _write(tmp_path, "dbt/models/staging/stg_orders__items.sql", "select 1 as id")
    return tmp_path


# ---- naming / lookup -------------------------------------------------------


@pytest.mark.parametrize("raw", ["customer_orders", "int_customer_orders", "Customer_Orders.sql"])
def test_normalize_intermediate_prefix(raw: str) -> None:
    assert normalize_model_name(raw, "intermediate") == "int_customer_orders"


@pytest.mark.parametrize("raw", ["../x", "a-b", "1abc", "", "a/b", "a\\b"])
def test_normalize_rejects_bad_names(raw: str) -> None:
    with pytest.raises(ModelServiceError):
        normalize_model_name(raw, "marts")


def test_normalize_unknown_layer_and_staging_prefix() -> None:
    with pytest.raises(ModelServiceError):
        normalize_model_name("x", "gold")
    with pytest.raises(ModelServiceError):
        normalize_model_name("orders", "staging")


def test_find_model_recursive_and_duplicate(proj: Path) -> None:
    _write(proj, "dbt/models/marts/finance/fct_revenue.sql")
    path, layer = find_model(proj, "fct_revenue")  # type: ignore[misc]
    assert layer == "marts" and path.name == "fct_revenue.sql"
    assert find_model(proj, "nope") is None
    _write(proj, "dbt/models/intermediate/fct_revenue.sql")
    with pytest.raises(ModelServiceError):
        find_model(proj, "fct_revenue")


# ---- extract_refs ----------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "refs", "sources"),
    [
        ("{{ ref('a') }}", ["a"], []),
        ('{{ ref("a") }}', ["a"], []),
        ("{{   ref(  'a'  )   }}", ["a"], []),
        ("{{ ref('pkg', 'a') }}", ["a"], []),
        ("{{ ref('a', v=2) }}", ["a"], []),
        ("{{ ref('a', version=2) }}", ["a"], []),
        ("{{ source('s', 't') }}", [], ["s.t"]),
        ('{{ source( "s" , "t" ) }}', [], ["s.t"]),
        (
            "{{ ref('a') }} {{ ref('a') }} {{ ref('b') }} {{ source('s','t') }}{{ source('s','t') }}",
            ["a", "b"],
            ["s.t"],
        ),
        ("-- ref('commented') outside jinja", [], []),
        ("{# {{ ref('jinja_comment') }} #}", [], []),
    ],
)
def test_extract_refs_forms(sql: str, refs: list[str], sources: list[str]) -> None:
    assert extract_refs(sql) == (refs, sources)


# ---- validate --------------------------------------------------------------


def test_validate_unknown_ref_with_suggestion(proj: Path) -> None:
    with pytest.raises(ModelServiceError) as ei:
        validate_model_sql(proj, "fct_x", "marts", "select * from {{ ref('stg_orders__order') }}")
    assert "did you mean 'stg_orders__orders'" in str(ei.value)


def test_validate_marts_source_call_rejected(proj: Path) -> None:
    with pytest.raises(ModelServiceError) as ei:
        validate_model_sql(proj, "fct_x", "marts", "select * from {{ source('orders', 'orders') }}")
    assert "ref the staging model (e.g. stg_orders__orders)" in str(ei.value)


def test_validate_intermediate_refs_marts_rejected(proj: Path) -> None:
    _write(proj, "dbt/models/marts/fct_a.sql")
    with pytest.raises(ModelServiceError) as ei:
        validate_model_sql(proj, "int_b", "intermediate", "select * from {{ ref('fct_a') }}")
    assert "must not ref a marts model" in str(ei.value)


def test_validate_staging_refs_downstream_rejected(proj: Path) -> None:
    _write(proj, "dbt/models/marts/fct_a.sql")
    with pytest.raises(ModelServiceError):
        validate_model_sql(
            proj, "stg_orders__orders", "staging", "select * from {{ ref('fct_a') }}"
        )


def test_validate_multiple_errors_collected(proj: Path) -> None:
    sql = "select * from {{ ref('ghost') }} {{ source('a', 'b') }} {{ ref('fct_x') }}"
    with pytest.raises(ModelServiceError) as ei:
        validate_model_sql(proj, "fct_x", "marts", sql)
    assert len(ei.value.errors) == 3


def test_validate_ref_cycle_rejected(proj: Path) -> None:
    _write(proj, "dbt/models/marts/cyc_a.sql", "select * from {{ ref('stg_orders__orders') }}")
    _write(proj, "dbt/models/marts/cyc_b.sql", "select * from {{ ref('cyc_a') }}")
    with pytest.raises(ModelServiceError) as ei:
        validate_model_sql(proj, "cyc_a", "marts", "select * from {{ ref('cyc_b') }}")
    assert "ref cycle: cyc_a -> cyc_b -> cyc_a" in str(ei.value)
    # a non-cyclic update of the same model is fine
    validate_model_sql(proj, "cyc_b", "marts", "select * from {{ ref('cyc_a') }}")


def test_validate_empty_sql(proj: Path) -> None:
    with pytest.raises(ModelServiceError):
        validate_model_sql(proj, "fct_x", "marts", "  \n")


def test_validate_seed_and_snapshot_refs_allowed(proj: Path) -> None:
    _write(proj, "dbt/seeds/countries.csv", "a\n1\n")
    _write(proj, "dbt/snapshots/snap_orders.sql", "select 1")
    sql = "select * from {{ ref('countries') }}, {{ ref('snap_orders') }}"
    validate_model_sql(proj, "fct_x", "marts", sql)


def test_validate_warnings(proj: Path) -> None:
    w = validate_model_sql(proj, "revenue", "marts", "select * from raw_orders.orders")
    assert any("raw_" in x for x in w)
    assert any("no ref()" in x for x in w)
    assert any("fct_" in x for x in w)
    w = validate_model_sql(proj, "fct_ok", "marts", "select * from {{ ref('stg_orders__orders') }}")
    assert w == []
    # staging updates legitimately touch raw tables / have no refs
    assert validate_model_sql(proj, "stg_orders__orders", "staging", "select * from raw_o.t") == []


# ---- render ----------------------------------------------------------------

_DATE = "2000-01-01"


def _freeze(text: str) -> str:
    import re

    return re.sub(r"-- Created: \d{4}-\d{2}-\d{2}", f"-- Created: {_DATE}", text)


def test_render_template_matches_wizard() -> None:
    # Expected text captured from the pre-refactor ModelWizard._generate_sql_template
    out = render_model_sql(
        "fct_x",
        "marts",
        upstream=["stg_stripe__customers", "stg_hubspot__customers"],
        description="My desc",
    )
    assert _freeze(out) == (
        "-- fct_x\n-- Created: 2000-01-01\n-- My desc\n\n{{ config(\n    materialized='table',\n"
        "    schema='marts'\n) }}\n\n\nWITH customers AS (\n"
        "    SELECT * FROM {{ ref('stg_stripe__customers') }}\n),\n"
        "stg_hubspot__customers AS (\n    SELECT * FROM {{ ref('stg_hubspot__customers') }}\n)\n\n"
        "SELECT\n    -- TODO: Define your transformation here\n    customers.*\nFROM customers\n"
    )
    plain = render_model_sql("int_x", "intermediate")
    assert _freeze(plain) == (
        "-- int_x\n-- Created: 2000-01-01\n\n{{ config(\n    materialized='table',\n"
        "    schema='intermediate'\n) }}\n\n\nSELECT\n    -- TODO: Define your transformation here\n"
        "    1 AS placeholder\n"
    )
    # alias collisions stay unique
    three = render_model_sql(
        "fct_y", "marts", upstream=["a_customers", "b_customers", "c_customers"]
    )
    assert "WITH customers AS" in three and "b_customers AS" in three and "c_customers AS" in three


def test_render_prepends_config_when_missing() -> None:
    out = render_model_sql("fct_x", "marts", sql="select 1", description="d")
    assert _freeze(out) == (
        "-- fct_x\n-- Created: 2000-01-01\n-- d\n\n{{ config(\n    materialized='table',\n"
        "    schema='marts'\n) }}\n\nselect 1\n"
    )


def test_render_keeps_existing_config() -> None:
    sql = "{{ config(materialized='table') }}\nselect 1\n\n\n"
    assert (
        render_model_sql("fct_x", "marts", sql=sql)
        == "{{ config(materialized='table') }}\nselect 1\n"
    )
    assert render_model_sql("stg_x", "staging", sql="select 1") == "select 1\n"


def test_render_rejects_view_materialization() -> None:
    with pytest.raises(ModelServiceError):
        render_model_sql("fct_x", "marts", materialization="view")


# ---- create / update -------------------------------------------------------

_SQL = "select id, sum(amount) as total from {{ ref('stg_orders__orders') }} group by id"


def test_create_model_with_sql_writes_file_and_docs(proj: Path) -> None:
    res = create_model(
        proj,
        "fct_orders",
        "marts",
        sql=_SQL,
        description="Orders",
        columns=[{"name": "id", "data_tests": ["unique", "not_null"]}],
        parse=False,
    )
    assert res.status == "created" and res.path == "dbt/models/marts/fct_orders.sql"
    assert res.refs == ["stg_orders__orders"]
    assert "config(" in (proj / res.path).read_text()
    data = yaml.safe_load((proj / "dbt/models/marts/schema.yml").read_text())
    entry = data["models"][0]
    assert entry["description"] == "Orders"
    assert entry["columns"] == [{"name": "id", "data_tests": ["unique", "not_null"]}]


def test_create_model_nothing_written_on_error(proj: Path) -> None:
    with pytest.raises(ModelServiceError):
        create_model(
            proj,
            "fct_bad",
            "marts",
            sql="select * from {{ ref('ghost') }}",
            description="d",
            parse=False,
        )
    assert not (proj / "dbt/models/marts").exists()
    with pytest.raises(ModelServiceError):
        create_model(proj, "fct_bad", "marts", upstream=["ghost"], parse=False)
    assert not (proj / "dbt/models/marts").exists()


def test_create_model_rejects_staging_and_collisions(proj: Path) -> None:
    with pytest.raises(ModelServiceError):
        create_model(proj, "stg_x__y", "staging", sql="select 1", parse=False)
    create_model(
        proj, "orders_summary", "intermediate", upstream=["stg_orders__orders"], parse=False
    )
    with pytest.raises(ModelServiceError):
        create_model(proj, "orders_summary", "intermediate", parse=False)
    with pytest.raises(ModelServiceError):
        create_model(proj, "int_orders_summary", "marts", parse=False)


def test_update_model_sql_and_docs_preserves_other_keys(proj: Path) -> None:
    _write(proj, "dbt/models/marts/fct_a.sql", "{{ config(materialized='table') }}\nselect 1 as id")
    schema = {
        "version": 2,
        "sources": [{"name": "s"}],
        "models": [
            {"name": "other", "config": {"tags": ["x"]}},
            {
                "name": "fct_a",
                "description": "old",
                "data_tests": [{"unique": {"column_name": "id"}}],
                "columns": [
                    {"name": "id", "description": "old id", "data_tests": ["not_null"]},
                    {"name": "amount", "meta": {"k": 1}},
                ],
            },
        ],
    }
    path = _write(proj, "dbt/models/marts/schema.yml", yaml.dump(schema, sort_keys=False))
    res = update_model(
        proj,
        "fct_a",
        columns=[{"name": "id", "description": "new id"}, {"name": "extra"}],
        parse=False,
    )
    assert res.status == "updated"
    out = yaml.safe_load(path.read_text())
    assert out["sources"] == [{"name": "s"}]
    assert out["models"][0] == schema["models"][0]
    fct = out["models"][1]
    assert fct["description"] == "old" and fct["data_tests"] == schema["models"][1]["data_tests"]
    assert fct["columns"][0] == {"name": "id", "description": "new id", "data_tests": ["not_null"]}
    assert fct["columns"][1] == {"name": "amount", "meta": {"k": 1}}
    assert fct["columns"][2] == {"name": "extra"}


def test_update_model_docs_located_in_nested_file(proj: Path) -> None:
    _write(
        proj, "dbt/models/marts/finance/fct_a.sql", "{{ config(materialized='table') }}\nselect 1"
    )
    nested = _write(
        proj,
        "dbt/models/marts/finance/docs.yml",
        yaml.dump({"version": 2, "models": [{"name": "fct_a"}]}),
    )
    update_model(proj, "fct_a", description="hello", parse=False)
    assert yaml.safe_load(nested.read_text())["models"][0]["description"] == "hello"
    assert not (proj / "dbt/models/marts/schema.yml").exists()


def test_update_model_errors(proj: Path) -> None:
    with pytest.raises(ModelServiceError) as ei:
        update_model(proj, "stg_orders__order", sql="select 1", parse=False)
    assert "did you mean" in str(ei.value)
    with pytest.raises(ModelServiceError):
        update_model(proj, "stg_orders__orders", parse=False)


def test_update_staging_strips_autogen_marker(proj: Path) -> None:
    path = _write(
        proj,
        "dbt/models/staging/stg_orders__orders.sql",
        "-- Auto-generated by Dango on 2026-01-01\nselect 1 as id",
    )
    gen = DbtModelGenerator.__new__(DbtModelGenerator)
    assert gen.is_auto_generated(path) is True
    res = update_model(
        proj,
        "stg_orders__orders",
        sql="-- Auto-generated by Dango on 2026-01-01\nselect 1 as id, 2 as n",
        parse=False,
    )
    assert gen.is_auto_generated(path) is False
    assert "select 1 as id, 2 as n" in path.read_text()
    assert any("auto-generated marker" in w for w in res.warnings)
    # staging docs go to stg_<source>.yml, never staging/schema.yml
    update_model(proj, "stg_orders__orders", description="d", parse=False)
    assert (proj / "dbt/models/staging/stg_orders.yml").exists()
    assert not (proj / "dbt/models/staging/schema.yml").exists()


# ---- parse rollback --------------------------------------------------------


def test_create_model_rolls_back_on_parse_failure(proj: Path) -> None:
    with patch.object(
        ms, "parse_project", side_effect=[(True, ""), (False, "Compilation Error boom")]
    ):
        with pytest.raises(ModelServiceError) as ei:
            create_model(
                proj, "fct_a", "marts", sql=_SQL, description="d", columns=[{"name": "id"}]
            )
    assert "Compilation Error boom" in str(ei.value)
    assert not (proj / "dbt/models/marts/fct_a.sql").exists()
    assert not (proj / "dbt/models/marts/schema.yml").exists()


def test_update_model_rolls_back_on_parse_failure(proj: Path) -> None:
    sql_path = _write(
        proj, "dbt/models/marts/fct_a.sql", "{{ config(materialized='table') }}\nselect 1"
    )
    yml = _write(
        proj,
        "dbt/models/marts/schema.yml",
        "version: 2\nmodels:\n- name: fct_a\n  description: orig\n",
    )
    before = (sql_path.read_bytes(), yml.read_bytes())
    with patch.object(
        ms, "parse_project", side_effect=[(True, ""), (False, "Compilation Error boom")]
    ):
        with pytest.raises(ModelServiceError):
            update_model(proj, "fct_a", sql="select 2", description="changed")
    assert (sql_path.read_bytes(), yml.read_bytes()) == before


def test_create_model_baseline_failure_writes_with_warning(proj: Path) -> None:
    with patch.object(ms, "parse_project", return_value=(False, "already broken")):
        res = create_model(proj, "fct_a", "marts", sql=_SQL)
    assert res.parse_ok is False
    assert any("already fails dbt parse" in w for w in res.warnings)
    assert (proj / "dbt/models/marts/fct_a.sql").exists()


def test_create_model_parse_success(proj: Path) -> None:
    with patch.object(ms, "parse_project", return_value=(True, "")) as p:
        res = create_model(proj, "fct_a", "marts", sql=_SQL)
    assert res.parse_ok is True and p.call_count == 2
