"""tests/unit/test_model_sql_guard.py

Model SQL guard: agent/service-created dbt models may not read files, URLs, settings or env vars.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from dango.transformation.generator import DbtModelGenerator
from dango.transformation.model_service import (
    ModelServiceError,
    create_model,
    update_model,
    validate_model_sql,
)

pytestmark = pytest.mark.unit

STG_SQL = "select 1 as id, 2 as amount, now() as ts"
LEAK_SQL = "select content from read_text('/etc/passwd')"

REJECT = [
    # file / URL readers
    "select content from read_text('/etc/passwd')",
    "select * from read_csv('x.csv')",
    "select * from read_csv_auto('x.csv')",
    "select * from read_parquet('s3://b/k')",
    "select * from read_json('x.json')",
    "select read_blob('x')",
    "select * from glob('/*')",
    "select * from 'data.csv'",
    "select * from '/etc/hosts'",
    'select * from "/etc/hosts"',
    "select * from parquet_scan('x')",
    "select * from iceberg_scan('x')",
    "select * from parquet_metadata('x')",
    "select * from st_read('x.shp')",
    "select * from sqlite_attach('x.db')",
    "select * from query('select 1')",
    "select * from query_table('t')",
    "select * from json_execute_serialized_sql("
    "json_serialize_sql('select content from read_text(''/etc/hosts'')'))",
    "select * from (select content from read_text('/etc/hosts')) s",
    "with a as (select * from read_text('/etc/hosts')) select * from a",
    "select * from {{ ref('stg_orders__orders') }} where id in (select 1 from read_text('x'))",
    # unquoted file names: DuckDB reads ./data.csv for `from data.csv`
    "select * from data.csv",
    "select * from x.json",
    "select * from raw_o.parquet",
    "select * from g.csv.gz",
    "select * from t.tsv",
    "select * from l.jsonl",
    # settings / environment / secrets
    "select getenv('HOME')",
    "select current_setting('home_directory')",
    "select getvariable('x')",
    "select * from duckdb_settings()",
    "select * from duckdb_secrets()",
    "select * from which_secret('s3://b', 's3')",
    "select * from duckdb_extensions()",
    # non-query statements
    "COPY (select 1) TO '/tmp/x'",
    "ATTACH '/tmp/a.duckdb'",
    "INSTALL httpfs",
    "LOAD json",
    "SET x=1",
    "PRAGMA version",
    "create table t as select 1",
    "insert into t select 1",
    "select 1; select 2",
    "select from where",
    # Jinja outside the allow-list
    "select {{ run_query('select 1') }}",
    "select '{{ env_var('X') }}' as v",
    'select \'{{ env_var("DANGO_SECRET", "d") }}\' as v',
    "select {{ var('x') }}",
    "{% set x = 1 %} select 1",
    "{%- set x = 1 -%} select 1",
    "{% for i in range(3) %} select {{ i }} {% endfor %}",
    "{% include 'x.sql' %}",
    "{% import 'm.sql' as m %} select 1",
    "{% raw %} select 1 {% endraw %}",
    "select {{ modules.datetime.datetime.now() }}",
    "select * from {{ adapter.get_relation('a', 'b', 'c') }}",
    "select {{ my_macro() }}",
    "select * from {{ ref('stg_orders__orders').identifier }}",
    "select * from {{ ref(x) }}",
    "-- {{ env_var('X') }}\nselect 1",
    # config(): only literal, non-executing keys
    "{{ config(pre_hook=\"copy (select 1) to '/tmp/x.csv'\") }} select 1",
    '{{ config(post_hook=["select 1"]) }} select 1',
    '{{ config(sql_header="set x=1") }} select 1',
    "{{ config(materialized='external', location='/tmp/x.csv') }} select 1",
    "{{ config(materialized=var('m')) }} select 1",
    "{{ config(schema=env_var('S')) }} select 1",
]


ACCEPT = [
    "select * from raw_orders.orders",
    "select * from staging.stg_x",
    "select * from warehouse.raw_orders.orders",
    "select * from {{ ref('stg_a__b') }}",
    'select * from {{ ref("stg_a__b") }}',
    "select * from {{ ref('pkg', 'stg_a__b') }}",
    "select * from {{ source('raw', 't') }} s join {{ ref('x') }} r using (id)",
    "select * from {{- ref('x') -}}",
    "with a as (select 1 x) select * from a",
    "select * from (values (1),(2)) v(x)",
    "select * from range(10)",
    "select * from generate_series(1, 3)",
    "select unnest([1,2])",
    "select * from unnest([1,2])",
    "pivot {{ ref('x') }} on region using sum(amount)",
    "select * from {{ ref('x') }} pivot (sum(amount) for region in ('eu', 'us'))",
    "select * exclude (a) from {{ ref('x') }} qualify row_number() over (partition by b) = 1",
    "select columns(*) from {{ ref('x') }}",
    "{{ config(materialized='table') }}\n"
    "with o as (select * from {{ ref('stg_a__orders') }}),\n"
    "c as (select * from {{ ref('stg_a__customers') }})\n"
    "select c.id, count(*) as n, sum(o.amount) over (partition by c.id) as running\n"
    "from o join c on o.customer_id = c.id group by all",
    "{{ config(materialized='incremental', unique_key='id', schema='marts', tags=['a']) }}\n"
    "select * from {{ ref('x') }}\n"
    "{% if is_incremental() %} where ts > (select max(ts) from {{ this }}) {% endif %}",
    "select * from {{ ref('x') }} {%- if is_incremental() %} where 1=1 {%- endif %}",
    "select 'read_text' as note",
    "select 'select * from read_text(''x'')' as note",
    "-- read_text('/etc/passwd') in a SQL comment\nselect 1",
    "{# {{ env_var('X') }} in a Jinja comment #}\nselect 1",
    "select \"glob\" from {{ ref('x') }}",
]


def _guard(sql: str) -> list[str]:
    from dango.transformation.model_sql_guard import check_model_sql

    return check_model_sql(sql)


@pytest.mark.parametrize("sql", REJECT)
def test_guard_rejects(sql: str) -> None:
    errors = _guard(sql)
    assert errors, f"expected rejection: {sql!r}"
    assert all(isinstance(e, str) and e for e in errors)


@pytest.mark.parametrize("sql", ACCEPT)
def test_guard_accepts(sql: str) -> None:
    assert _guard(sql) == []


def test_guard_message_names_function_and_next_step() -> None:
    msg = " ".join(_guard("select content from read_text('/etc/passwd')"))
    assert "read_text" in msg
    assert "dango source add" in msg


def test_guard_message_names_jinja_construct() -> None:
    msg = " ".join(_guard("select '{{ env_var('X') }}' as v"))
    assert "Jinja construct not allowed" in msg
    assert "env_var" in msg


def test_guard_unparsable_fails_closed() -> None:
    assert _guard("select from where") == ["Could not verify this SQL safely; simplify it"]


def test_guard_bare_glob_column_fails_closed_quoted_ok() -> None:
    # sqlglot treats unquoted GLOB as an operator keyword, so this cannot be verified;
    # quoting the column name is the documented workaround.
    assert _guard("select glob from {{ ref('x') }}")
    assert _guard("select \"glob\" from {{ ref('x') }}") == []


def test_generated_staging_models_pass(tmp_path: Path) -> None:
    gen = DbtModelGenerator(tmp_path)
    src = SimpleNamespace(name="shop", type=SimpleNamespace(value="csv"))
    sql = gen.generate_staging_model(
        src,
        "orders",
        schema_name="raw_shop",
        date_cast_columns=["Created At"],
        staging_columns=[
            {"name": "id", "type": "BIGINT"},
            {"name": "Created At", "type": "TIMESTAMP"},
            {"name": "order-total", "type": "VARCHAR"},
        ],
    )
    assert _guard(sql) == []
    geo = (gen.templates_dir / "stg_geo_names.sql").read_text().replace("__SOURCE_NAME__", "ads")
    assert _guard(geo) == []


# ---- service level ---------------------------------------------------------


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _write(tmp_path, "dbt/models/staging/stg_orders__orders.sql", STG_SQL)

    def _no_parse(root: Path) -> tuple[bool, str]:
        raise AssertionError("dbt parse gate reached for rejected SQL")

    monkeypatch.setattr("dango.transformation.model_service.parse_project", _no_parse)
    return tmp_path


def _tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_create_model_rejects_file_read(proj: Path) -> None:
    before = _tree(proj)
    with pytest.raises(ModelServiceError) as ei:
        create_model(proj, "leak_probe", "marts", sql=LEAK_SQL, description="d")
    assert any("read_text" in e for e in ei.value.errors)
    assert not (proj / "dbt/models/marts/leak_probe.sql").exists()
    assert _tree(proj) == before


def test_update_model_rejects_file_read(proj: Path) -> None:
    before = _tree(proj)
    with pytest.raises(ModelServiceError) as ei:
        update_model(proj, "stg_orders__orders", sql=LEAK_SQL)
    assert any("read_text" in e for e in ei.value.errors)
    assert _tree(proj) == before


ENV_VAR_SQL = [
    "select '{{ env_var('API_KEY') }}' as v",
    'select \'{{ env_var("API_KEY", "fallback") }}\' as v',
    "-- {{ env_var('API_KEY') }}\nselect 1 as v",
    "select 1 as v {{- env_var('API_KEY') -}}",
]


@pytest.mark.parametrize("sql", ENV_VAR_SQL)
def test_create_and_update_model_reject_env_var(proj: Path, sql: str) -> None:
    before = _tree(proj)
    with pytest.raises(ModelServiceError) as ei:
        create_model(proj, "env_probe", "marts", sql=sql)
    assert any("env_var" in e for e in ei.value.errors)
    with pytest.raises(ModelServiceError) as ei:
        update_model(proj, "stg_orders__orders", sql=sql)
    assert any("env_var" in e for e in ei.value.errors)
    assert _tree(proj) == before


def test_validate_warnings_survive_guard(proj: Path) -> None:
    w = validate_model_sql(proj, "revenue", "marts", "select * from raw_orders.orders")
    assert any("raw_" in x for x in w)
    assert validate_model_sql(proj, "stg_orders__orders", "staging", "select * from raw_o.t") == []


def test_validate_rejects_file_extension_table(proj: Path) -> None:
    with pytest.raises(ModelServiceError) as ei:
        validate_model_sql(proj, "fct_x", "marts", "select * from raw_api.json")
    assert any("json" in e for e in ei.value.errors)


def test_mcp_create_model_returns_error_dict(
    proj: Path, sample_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dango.cli.commands import mcp_models
    from dango.config.helpers import save_config

    save_config(sample_config, proj)
    monkeypatch.setattr(mcp_models, "_get_project_root", lambda: proj)
    monkeypatch.setattr(mcp_models, "_git_warnings", lambda project_root: [])
    before = _tree(proj)
    result = mcp_models.create_model("leak_probe", "marts", sql=LEAK_SQL)
    assert set(result) == {"error", "errors"}
    assert isinstance(result["errors"], list) and result["errors"]
    assert result["error"] == "; ".join(result["errors"])
    assert _tree(proj) == before
