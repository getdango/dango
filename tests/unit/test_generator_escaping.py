"""tests/unit/test_generator_escaping.py

Generated staging SQL quotes reserved words, and generated SQL/YAML never lets
data-derived strings (column/table names, descriptions) be rendered by dbt's Jinja.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

from dango.config.models import DataSource, SourceType
from dango.transformation.generator import DbtModelGenerator, _quote_column_name

CANARY_VALUE = "CANARY_SECRET_9f3a"
HOSTILE_NAMES = [
    "{{ env_var('CANARY') }}",
    "{% if true %}x{% endif %}",
    "{# c #}",
    "a {% endraw %} {{ env_var('CANARY') }}",
    "{{{ env_var('CANARY') }}}",
    "x{",
    "tail {",
    "{{",
]


def _source() -> DataSource:
    return DataSource(name="payments", type=SourceType.STRIPE)


@pytest.mark.unit
class TestReservedWordQuoting:
    """K-21: reserved words and digit-leading names are quoted; quotes are escaped."""

    @pytest.mark.parametrize("name", ["order", "select", "GROUP", "from", "user", "2fa", 'a"b'])
    def test_unsafe_names_are_quoted(self, name: str) -> None:
        quoted = _quote_column_name(name)
        assert quoted.startswith('"') and quoted.endswith('"')
        assert quoted == '"' + name.replace('"', '""') + '"'

    @pytest.mark.parametrize("name", ["id", "amount", "_dango_loaded_at", "col_1"])
    def test_plain_names_stay_unquoted(self, name: str) -> None:
        assert _quote_column_name(name) == name

    def test_generated_model_executes_in_duckdb(self, tmp_path: Path) -> None:
        columns = ["order", "select", "2fa", 'a"b', "id", "left", "user"]
        db = tmp_path / "w.duckdb"
        conn = duckdb.connect(str(db))
        conn.execute("CREATE SCHEMA raw_payments")
        cols_ddl = ", ".join('"' + c.replace('"', '""') + '" INTEGER' for c in columns)
        conn.execute(f"CREATE TABLE raw_payments.t ({cols_ddl})")
        conn.execute("INSERT INTO raw_payments.t VALUES (1, 2, 3, 4, 5, 6, 7)")

        gen = DbtModelGenerator(tmp_path)
        sql = gen.generate_staging_model(
            _source(),
            "t",
            schema_name="raw_payments",
            staging_columns=[{"name": c, "type": "INTEGER"} for c in columns],
            date_cast_columns=["order"],
        )
        # Resolve the dbt-only constructs so DuckDB can run the SELECT directly.
        sql = re.sub(r"\{\{\s*config\(.*?\)\s*\}\}", "", sql, flags=re.S)
        sql = re.sub(r"\{\{\s*source\([^)]*\)\s*\}\}", "raw_payments.t", sql)
        rows = conn.execute(sql).fetchall()
        assert len(rows) == 1
        conn.close()

    def test_every_non_unreserved_duckdb_keyword_is_quoted(self) -> None:
        conn = duckdb.connect(":memory:")
        words = conn.execute(
            "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category <> 'unreserved'"
        ).fetchall()
        assert words
        for (word,) in words:
            assert _quote_column_name(word).startswith('"'), word


@pytest.mark.unit
class TestJinjaNeutralisedInGeneratedText:
    """K-22: no data-derived string reaches dbt as renderable Jinja."""

    def test_generated_yaml_round_trips_and_has_no_live_jinja(self, tmp_path: Path) -> None:
        gen = DbtModelGenerator(tmp_path)
        for hostile in HOSTILE_NAMES:
            tables = [
                {
                    "name": hostile,
                    "columns": [
                        {"name": hostile, "description": hostile, "tests": []},
                    ],
                }
            ]
            text = gen.generate_sources_yml(_source(), "raw_payments", tables)
            data = yaml.safe_load(text)  # must stay valid YAML
            table = data["sources"][0]["tables"][0]
            assert table["name"]
            assert "{{ env_var" not in text
            assert "{% if" not in text and "{# c #}" not in text

    def test_staging_schema_yaml_has_no_live_jinja(self, tmp_path: Path) -> None:
        gen = DbtModelGenerator(tmp_path)
        for hostile in HOSTILE_NAMES:
            models = [
                {
                    "name": "stg_" + hostile,
                    "description": hostile,
                    "columns": [{"name": hostile, "tests": []}],
                }
            ]
            text = gen.generate_staging_schema_yml(_source(), models)
            yaml.safe_load(text)
            assert "{{ env_var" not in text
            assert "{% if" not in text and "{# c #}" not in text

    def test_staging_sql_has_no_live_jinja_outside_config_and_source(self, tmp_path: Path) -> None:
        gen = DbtModelGenerator(tmp_path)
        sql = gen.generate_staging_model(
            _source(),
            "{{ env_var('CANARY') }}",
            schema_name="raw_payments",
            staging_columns=[{"name": "{{ env_var('CANARY') }}", "type": "VARCHAR"}],
        )
        assert "{{ env_var" not in sql

    def test_newline_in_name_cannot_escape_sql_comment(self, tmp_path: Path) -> None:
        gen = DbtModelGenerator(tmp_path)
        sql = gen.generate_staging_model(
            _source(),
            "t\nDROP TABLE x;--",
            schema_name="raw_payments",
            staging_columns=[],
        )
        assert "\nDROP TABLE x" not in sql


@pytest.mark.unit
class TestDbtDoesNotEvaluateGeneratedText:
    """A real ``dbt parse`` of generated files must not evaluate data-derived Jinja."""

    def test_dbt_parse_keeps_names_literal_and_leaks_nothing(self, tmp_path: Path) -> None:
        dbt_dir = tmp_path / "dbt"
        staging = dbt_dir / "models" / "staging"
        staging.mkdir(parents=True)
        (dbt_dir / "dbt_project.yml").write_text(
            "name: t\nversion: '1.0.0'\nprofile: t\nmodel-paths: [models]\n"
        )
        (dbt_dir / "profiles.yml").write_text(
            "t:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
            f"      path: {tmp_path / 'w.duckdb'}\n"
        )
        hostile = "{{ env_var('CANARY') }}"
        gen = DbtModelGenerator(tmp_path)
        source = DataSource(name="payments", type=SourceType.STRIPE)
        (staging / "sources_payments.yml").write_text(
            gen.generate_sources_yml(
                source,
                "raw_payments",
                [
                    {
                        "name": hostile,
                        "columns": [
                            {"name": hostile, "description": hostile, "tests": []},
                            {"name": "{% if true %}x{% endif %}", "tests": []},
                        ],
                    }
                ],
            )
        )
        (staging / "stg_payments.yml").write_text(
            gen.generate_staging_schema_yml(
                source,
                [
                    {
                        "name": "stg_payments__t",
                        "description": hostile,
                        "columns": [{"name": hostile, "tests": []}],
                    }
                ],
            )
        )
        (staging / "stg_payments__t.sql").write_text(
            gen.generate_staging_model(
                source,
                hostile,
                schema_name="raw_payments",
                staging_columns=[{"name": hostile, "type": "VARCHAR"}],
            )
        )

        env = dict(os.environ)
        env.update(
            CANARY=CANARY_VALUE,
            HOME=str(tmp_path / "home"),
            DBT_SEND_ANONYMOUS_USAGE_STATS="False",
        )
        dbt = Path(sys.executable).parent / "dbt"
        result = subprocess.run(
            [str(dbt), "parse", "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        manifest_text = (dbt_dir / "target" / "manifest.json").read_text()
        assert CANARY_VALUE not in manifest_text
        manifest = json.loads(manifest_text)
        source_names = {s["name"] for s in manifest["sources"].values()}
        assert hostile in source_names
        col_names = {c for s in manifest["sources"].values() for c in s["columns"]}
        assert hostile in col_names
        assert "{% if true %}x{% endif %}" in col_names


@pytest.mark.unit
class TestSchemaSyncNeutralises:
    """Post-build schema sync and post-sync enrichment write/compare neutralised names."""

    def test_schema_manager_neutralises_new_column_and_is_stable(self, tmp_path: Path) -> None:
        from dango.cli.schema_manager import SchemaManager

        mgr = SchemaManager(tmp_path, tmp_path / "w.duckdb")
        cols = [{"name": "{{ env_var('CANARY') }}", "type": "VARCHAR"}]
        schema, _ = mgr._merge_schema("m", cols, None, "marts")
        text = yaml.dump(schema)
        assert "{{ env_var" not in text
        # A second sync against the file it wrote must report no churn.
        schema2, changes = mgr._merge_schema("m", cols, schema, "marts")
        assert changes["added_columns"] == []
        assert changes["removed_columns"] == []
