"""tests/integration/dbt_outcome_support.py

Real-dbt project builders for the dbt build outcome tests: a hand-built warehouse project
(one variant per outcome) and a CSV project that the real `run_sync` loads.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

VARIANTS = ("clean", "test_only", "warn_only", "model_error", "skipped")

_PROJECT_YML = "name: t\nversion: '1.0.0'\nprofile: t\nmodel-paths: [models]\n"

_SCHEMA = {
    # the leaf model's own test fails (plus a warn-only one); nothing runs downstream of it
    "test_only": (
        "version: 2\nmodels:\n  - name: order_count\n    columns:\n      - name: n\n"
        "        tests:\n          - accepted_values:\n              values: [99]\n"
        "          - accepted_values:\n              values: [98]\n"
        "              config:\n                severity: warn\n"
    ),
    "warn_only": (
        "version: 2\nmodels:\n  - name: order_count\n    columns:\n      - name: n\n"
        "        tests:\n          - accepted_values:\n              values: [99]\n"
        "              config:\n                severity: warn\n"
    ),
    # duplicate ids make the staging key test fail; the mart below it is skipped
    "skipped": (
        "version: 2\nmodels:\n  - name: stg_orders\n    columns:\n      - name: id\n"
        "        tests: [unique]\n"
    ),
}


def make_matrix_project(root: Path, variant: str) -> Path:
    """A warehouse with raw_shop.orders and a dbt project whose outcome is set by `variant`."""
    assert variant in VARIANTS
    (root / "data").mkdir(parents=True)
    conn = duckdb.connect(str(root / "data" / "warehouse.duckdb"))
    conn.execute("CREATE SCHEMA raw_shop")
    conn.execute("CREATE TABLE raw_shop.orders (id INTEGER)")
    rows = "(1), (2), (3)" if variant != "skipped" else "(1), (2), (2)"
    conn.execute(f"INSERT INTO raw_shop.orders VALUES {rows}")
    conn.close()

    dbt_dir = root / "dbt"
    models = dbt_dir / "models"
    models.mkdir(parents=True)
    (dbt_dir / "dbt_project.yml").write_text(_PROJECT_YML, encoding="utf-8", newline="\n")
    (dbt_dir / "profiles.yml").write_text(
        "t:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {(root / 'data' / 'warehouse.duckdb').as_posix()}\n",
        encoding="utf-8",
        newline="\n",
    )
    (models / "stg_orders.sql").write_text(
        "{{ config(materialized='table') }}\nselect id from raw_shop.orders\n",
        encoding="utf-8",
        newline="\n",
    )
    (models / "order_count.sql").write_text(
        "{{ config(materialized='table') }}\nselect count(*) as n from {{ ref('stg_orders') }}\n",
        encoding="utf-8",
        newline="\n",
    )
    if variant == "model_error":
        (models / "broken_mart.sql").write_text(
            "{{ config(materialized='table') }}\nselect cast('abc' as integer) as x\n",
            encoding="utf-8",
            newline="\n",
        )
    if variant in _SCHEMA:
        (models / "schema.yml").write_text(_SCHEMA[variant], encoding="utf-8", newline="\n")
    return root


def make_csv_project(root: Path, csv_text: str, *, failing_leaf_test: bool = False) -> Path:
    """A CSV project (source `shop`) with a staging layer, a mart and no Docker.

    `failing_leaf_test` adds a data test on the mart that always fails (nothing runs below it).
    """
    (root / "data" / "uploads").mkdir(parents=True)
    (root / ".dango").mkdir()
    (root / ".dango" / "auth.db").touch()  # v1 marker: a project that is not a v0.1.x one
    (root / ".dango" / "project.yml").write_text(
        "project:\n  name: proj\n  created_by: test\n  purpose: test\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / ".dango" / "sources.yml").write_text(
        "version: '1.0'\nsources:\n  - name: shop\n    type: csv\n"
        "    csv:\n      directory: data/uploads\n",
        encoding="utf-8",
        newline="\n",
    )
    models = root / "dbt" / "models"
    (models / "staging").mkdir(parents=True)
    (models / "marts").mkdir(parents=True)
    (root / "dbt" / "macros").mkdir()
    (root / "dbt" / "dbt_project.yml").write_text(
        "name: 'proj'\nversion: '1.0.0'\nconfig-version: 2\nprofile: 'proj'\n"
        'model-paths: ["models"]\nmacro-paths: ["macros"]\ntarget-path: "target"\n'
        "models:\n  proj:\n    staging:\n      +materialized: table\n      +schema: staging\n"
        "    marts:\n      +materialized: table\n      +schema: marts\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "dbt" / "profiles.yml").write_text(
        "proj:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        "      path: ../data/warehouse.duckdb\n      schema: main\n      threads: 4\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / "dbt" / "macros" / "get_custom_schema.sql").write_text(
        "{% macro generate_schema_name(custom_schema_name, node) -%}"
        "{%- if custom_schema_name is none -%}{{ target.schema }}"
        "{%- else -%}{{ custom_schema_name | trim }}{%- endif -%}{%- endmacro %}\n",
        encoding="utf-8",
        newline="\n",
    )
    (models / "marts" / "order_count.sql").write_text(
        "select count(*) as n from {{ ref('stg_shop__shop') }}\n", encoding="utf-8", newline="\n"
    )
    if failing_leaf_test:
        (models / "marts" / "schema.yml").write_text(
            "version: 2\nmodels:\n  - name: order_count\n    columns:\n      - name: n\n"
            "        tests:\n          - accepted_values:\n              values: [99]\n",
            encoding="utf-8",
            newline="\n",
        )
    write_orders_csv(root, csv_text)
    return root


def write_orders_csv(root: Path, csv_text: str) -> None:
    (root / "data" / "uploads" / "orders.csv").write_text(csv_text, encoding="utf-8", newline="\n")
