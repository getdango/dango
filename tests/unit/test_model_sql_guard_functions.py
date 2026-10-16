"""tests/unit/test_model_sql_guard_functions.py

Function allow-list of the model SQL guard: realistic analytic SQL passes, unknown functions fail closed.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

REF = "{{ ref('stg_shop__orders') }}"

CORPUS = [
    # aggregates, grouping, approx
    f"select customer_id, count(*) as n, count(distinct sku) as skus, sum(amount) as total, "
    f"avg(amount) as mean, min(ts) as first_ts, max(ts) as last_ts, "
    f"approx_count_distinct(session_id) as sessions, median(amount) as med, "
    f"quantile_cont(amount, 0.9) as p90, string_agg(sku, ',') as skus_csv, "
    f"array_agg(sku) as sku_list, bool_and(paid) as all_paid, any_value(region) as region "
    f"from {REF} group by all",
    # windows + qualify
    f"select *, row_number() over (partition by customer_id order by ts) as rn, "
    f"rank() over (order by amount desc) as rk, dense_rank() over (order by amount) as drk, "
    f"lag(amount) over (partition by customer_id order by ts) as prev_amount, "
    f"lead(ts, 1) over (partition by customer_id order by ts) as next_ts, "
    f"first_value(sku) over w as first_sku, last_value(sku) over w as last_sku, "
    f"ntile(4) over (order by amount) as quartile, "
    f"percent_rank() over (order by amount) as pr, cume_dist() over (order by amount) as cd, "
    f"sum(amount) over (partition by customer_id order by ts "
    f"rows between unbounded preceding and current row) as running "
    f"from {REF} window w as (partition by customer_id order by ts) qualify rn = 1",
    # date math
    f"select date_trunc('month', ts) as month, date_part('year', ts) as yr, "
    f"extract(dow from ts) as dow, strftime(ts, '%Y-%m-%d') as day, "
    f"strptime('2024-01-02', '%Y-%m-%d') as parsed, date_diff('day', ts, now()) as age_days, "
    f"datediff('month', ts, current_date) as months, ts + interval 7 day as week_later, "
    f"make_date(2024, 1, 1) as d0, last_day(ts::date) as eom, dayname(ts) as dn, "
    f"epoch(ts) as e, to_timestamp(1700000000) as t0, current_timestamp as now_ts, "
    f"age(ts) as age_iv, cast(ts as date) as d, try_cast(raw_amount as decimal(18, 2)) as amt "
    f"from {REF}",
    # strings, regex, conditionals
    f"select lower(trim(email)) as email, upper(left(country, 2)) as cc, "
    f"split_part(email, '@', 2) as domain, length(sku) as sku_len, concat_ws('-', a, b) as ab, "
    f"regexp_replace(phone, '[^0-9]', '', 'g') as digits, "
    f"regexp_extract(url, 'utm_source=([^&]+)', 1) as utm, regexp_matches(sku, '^A') as is_a, "
    f"coalesce(region, 'unknown') as region, nullif(amount, 0) as nz, "
    f"case when amount > 100 then 'big' else 'small' end as size, "
    f"if(paid, 'yes', 'no') as paid_label, greatest(a, b) as g, least(a, b) as l, "
    f"round(amount, 2) as r, abs(delta) as ad, md5(email) as email_hash, hash(sku) as h, "
    f"replace(name, 'x', 'y') as nm, contains(name, 'pro') as is_pro, "
    f"starts_with(sku, 'A') as sa, substring(sku, 1, 3) as prefix, sku like 'A%' as l2 "
    f"from {REF}",
    # structs, lists, maps
    f"select struct_pack(id := id, amount := amount) as s, {{'a': 1, 'b': 2}} as lit, "
    f"[1, 2, 3] as arr, list_value(a, b) as lv, list_transform(tags, x -> upper(x)) as up, "
    f"list_filter(tags, x -> x <> '') as nonempty, len(tags) as n_tags, "
    f"list_contains(tags, 'vip') as vip, array_length(tags) as alen, unnest(tags) as tag, "
    f"s.amount as s_amount, tags[1] as first_tag, map_keys(attrs) as keys "
    f"from {REF}",
    # ranges, generate_series, set ops, exists
    "select * from range(10) t(i)",
    "select generate_series(1, 3) as xs",
    f"select id from {REF} where exists (select 1 from {REF} o2 where o2.id = id) "
    f"union all select 1 except select 2",
    f"select columns('amount_.*') from {REF}",
    f"select id, grouping(region) as g from {REF} group by rollup (region, id)",
]


def _guard(sql: str) -> list[str]:
    from dango.transformation.model_sql_guard import check_model_sql

    return check_model_sql(sql)


@pytest.mark.parametrize("sql", CORPUS)
def test_realistic_analytic_sql_passes(sql: str) -> None:
    assert _guard(sql) == []


@pytest.mark.parametrize(
    ("sql", "name"),
    [
        (f"select totally_unknown_fn(id) from {REF}", "totally_unknown_fn"),
        (f"select st_area(geom) from {REF}", "st_area"),
        (f"select http_get('https://example.com') from {REF}", "http_get"),
        (f"select my_macro(1) from {REF}", "my_macro"),
        (f"select upper(totally_unknown_fn(id)) from {REF}", "totally_unknown_fn"),
    ],
)
def test_function_not_on_allowlist_is_refused(sql: str, name: str) -> None:
    errors = _guard(sql)
    assert errors
    msg = " ".join(errors)
    assert name in msg
    assert "allow-list" in msg


def test_shipped_allowlist_excludes_denied_names() -> None:
    from dango.transformation.model_sql_guard import is_denied_function, load_function_allowlist

    allow = load_function_allowlist()
    assert len(allow) > 500
    assert not [n for n in allow if is_denied_function(n)]
    for denied in ("read_text", "read_csv", "glob", "current_setting", "getvariable", "getenv"):
        assert denied not in allow
    for needed in (
        "sum",
        "count",
        "date_trunc",
        "row_number",
        "approx_count_distinct",
        "struct_pack",
        "list_transform",
        "regexp_replace",
        "strftime",
        "generate_series",
    ):
        assert needed in allow


def test_allowlist_file_header_and_order() -> None:
    from importlib import resources

    text = (
        resources.files("dango.transformation")
        .joinpath("duckdb_function_allowlist.txt")
        .read_text(encoding="utf-8")
    )
    header = [ln for ln in text.splitlines() if ln.startswith("#")]
    names = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert any("DuckDB 1.5" in ln for ln in header)
    assert any("generate_duckdb_function_allowlist.py" in ln for ln in header)
    assert names == sorted(set(names))
