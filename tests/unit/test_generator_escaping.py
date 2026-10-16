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

    @pytest.mark.parametrize("name", ["order", "select", "GROUP", "from", "left", "2fa", 'a"b'])
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
        cols_ddl = ", ".join('"' + c.replace('"', '""') + '" VARCHAR' for c in columns)
        conn.execute(f"CREATE TABLE raw_payments.t ({cols_ddl})")
        conn.execute(
            "INSERT INTO raw_payments.t VALUES ('2020-01-01', 'b', 'c', 'd', 'e', 'f', 'g')"
        )

        gen = DbtModelGenerator(tmp_path)
        sql = gen.generate_staging_model(
            _source(),
            "t",
            schema_name="raw_payments",
            staging_columns=[{"name": c, "type": "VARCHAR"} for c in columns],
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
        # The only live Jinja left is dbt's own config()/source() calls.
        body = re.sub(r"\{\{\s*(config|source)\(.*?\)\s*\}\}", "", sql, flags=re.S)
        assert "{{ env_var" not in body.replace("{{ '{' }}", "")
        assert "{{ env_var" not in sql.split("WITH source")[0].replace("{{ '{' }}", "")

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


@pytest.mark.unit
class TestEscapingHelpers:
    """Helper output renders back to the original text under Jinja."""

    @pytest.mark.parametrize("value", HOSTILE_NAMES + ["plain", "it's", 'say "hi"'])
    def test_neutralised_text_renders_back_to_original(self, value: str) -> None:
        import jinja2

        from dango.utils.dbt_escaping import neutralize_jinja, yaml_scalar

        env = jinja2.Environment()
        assert env.from_string(neutralize_jinja(value)).render() == value
        loaded = yaml.safe_load("k: " + yaml_scalar(value))["k"]
        assert env.from_string(loaded).render() == value

    @pytest.mark.parametrize("word", ["yes", "No", "null", "true", "on", "7up", "a b", "x: y"])
    def test_yaml_scalar_keeps_strings_as_strings(self, word: str) -> None:
        from dango.utils.dbt_escaping import yaml_scalar

        assert yaml.safe_load("k: " + yaml_scalar(word))["k"] == word

    @pytest.mark.parametrize("name", ["it's", 'say "hi"', "{{ x }}"])
    def test_source_literal_is_valid_jinja(self, name: str) -> None:
        import jinja2

        from dango.utils.dbt_escaping import jinja_string_literal

        env = jinja2.Environment()
        assert env.from_string("{{ " + jinja_string_literal(name) + " }}").render() == name


@pytest.mark.unit
class TestPostSyncEnrichmentWithDelimiters:
    """_enrich_staging_tests matches neutralised column names written by the generator."""

    def test_not_null_added_to_neutralised_column(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from dango.utils.dbt_escaping import neutralize_jinja
        from dango.utils.post_sync import _enrich_staging_tests

        hostile = "{{ env_var('CANARY') }}"
        staging_dir = tmp_path / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)
        (staging_dir / "stg_shop.yml").write_text(
            yaml.dump(
                {
                    "models": [
                        {
                            "name": "stg_shop__orders",
                            "columns": [{"name": neutralize_jinja(hostile)}],
                        }
                    ]
                }
            )
        )
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [(hostile,)]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("dango.utils.post_sync.connect", return_value=mock_conn):
            _enrich_staging_tests(tmp_path, ["shop"])
        data = yaml.safe_load((staging_dir / "stg_shop.yml").read_text())
        assert data["models"][0]["columns"][0]["tests"] == ["not_null"]


def _write_dbt_project(tmp_path: Path) -> Path:
    dbt_dir = tmp_path / "dbt"
    (dbt_dir / "models" / "staging").mkdir(parents=True)
    (dbt_dir / "dbt_project.yml").write_text(
        "name: t\nversion: '1.0.0'\nprofile: t\nmodel-paths: [models]\n"
    )
    (dbt_dir / "profiles.yml").write_text(
        "t:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n"
        f"      path: {tmp_path / 'w.duckdb'}\n"
    )
    return dbt_dir


def _run_dbt(dbt_dir: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.update(
        CANARY=CANARY_VALUE,
        HOME=str(tmp_path / "home"),
        DBT_SEND_ANONYMOUS_USAGE_STATS="False",
    )
    dbt = Path(sys.executable).parent / "dbt"
    return subprocess.run(
        [str(dbt), *args, "--project-dir", str(dbt_dir), "--profiles-dir", str(dbt_dir)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.unit
class TestGenericTestsOnKeywordColumns:
    """not_null tests on columns named like keywords build under a real ``dbt build``."""

    def test_dbt_build_passes_with_keyword_columns(self, tmp_path: Path) -> None:
        columns = ["order", "2fa", "left"]
        conn = duckdb.connect(str(tmp_path / "w.duckdb"))
        conn.execute("CREATE SCHEMA raw_payments")
        ddl = ", ".join(f'"{c}" VARCHAR' for c in columns)
        conn.execute(f"CREATE TABLE raw_payments.t ({ddl})")
        conn.execute("INSERT INTO raw_payments.t VALUES ('a', 'b', 'c')")
        conn.close()

        dbt_dir = _write_dbt_project(tmp_path)
        staging = dbt_dir / "models" / "staging"
        gen = DbtModelGenerator(tmp_path)
        source = _source()
        cols = [{"name": c, "tests": ["not_null"], "description": f"{c} column"} for c in columns]
        (staging / "sources_payments.yml").write_text(
            gen.generate_sources_yml(source, "raw_payments", [{"name": "t", "columns": cols}])
        )
        (staging / "stg_payments.yml").write_text(
            gen.generate_staging_schema_yml(
                source,
                [{"name": "stg_payments__t", "description": "d", "columns": cols}],
            )
        )
        (staging / "stg_payments__t.sql").write_text(
            gen.generate_staging_model(
                source,
                "t",
                schema_name="raw_payments",
                staging_columns=[{"name": c, "type": "VARCHAR"} for c in columns],
            )
        )
        result = _run_dbt(dbt_dir, tmp_path, "build")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_schema_manager_marks_keyword_columns_quoted(self, tmp_path: Path) -> None:
        from dango.cli.schema_manager import SchemaManager

        mgr = SchemaManager(tmp_path, tmp_path / "w.duckdb")
        cols = [{"name": "order", "type": "VARCHAR"}, {"name": "id", "type": "VARCHAR"}]
        schema, _ = mgr._merge_schema("m", cols, None, "marts")
        by_name = {c["name"]: c for c in schema["models"][0]["columns"]}
        assert by_name["order"]["quote"] is True
        assert "quote" not in by_name["id"]

    def test_post_sync_enrichment_marks_keyword_columns_quoted(self, tmp_path: Path) -> None:
        from unittest.mock import MagicMock, patch

        from dango.utils.post_sync import _enrich_staging_tests

        staging_dir = tmp_path / "dbt" / "models" / "staging"
        staging_dir.mkdir(parents=True)
        (staging_dir / "stg_shop.yml").write_text(
            yaml.dump({"models": [{"name": "stg_shop__o", "columns": [{"name": "order"}]}]})
        )
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = [("order",)]
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        with patch("dango.utils.post_sync.connect", return_value=mock_conn):
            _enrich_staging_tests(tmp_path, ["shop"])
        col = yaml.safe_load((staging_dir / "stg_shop.yml").read_text())["models"][0]["columns"][0]
        assert col["tests"] == ["not_null"]
        assert col["quote"] is True


@pytest.mark.unit
class TestSnapshotTemplateEscaping:
    """snapshot.sql.j2 never lets data-derived names be rendered as Jinja."""

    def test_snapshot_comment_and_source_call_are_neutralised(self, tmp_path: Path) -> None:
        from dango.cli.commands.snapshot import _render_snapshot

        hostile = "{{ env_var('CANARY') }}"
        rendered = _render_snapshot(
            generated_at="now",
            snapshot_name="snap_t",
            source_name="payments",
            table_name=hostile,
            strategy="check",
            unique_key="id",
            updated_at="",
        )
        assert "{{ env_var" not in rendered.replace("{{ '{' }}", "")
        dbt_dir = _write_dbt_project(tmp_path)
        (dbt_dir / "snapshots").mkdir()
        (dbt_dir / "snapshots" / "snap_t.sql").write_text(rendered)
        (dbt_dir / "models" / "staging" / "s.yml").write_text(_source_yml_for(tmp_path, hostile))
        result = _run_dbt(dbt_dir, tmp_path, "parse")
        assert result.returncode == 0, result.stdout + result.stderr
        assert CANARY_VALUE not in (dbt_dir / "target" / "manifest.json").read_text()


def _source_yml_for(tmp_path: Path, table: str) -> str:
    return DbtModelGenerator(tmp_path).generate_sources_yml(
        _source(), "raw_payments", [{"name": table, "columns": []}]
    )


@pytest.mark.unit
class TestControlCharacters:
    """Control characters in data-derived names cannot break YAML or ``dbt parse``."""

    def test_dbt_parse_survives_control_characters(self, tmp_path: Path) -> None:
        dbt_dir = _write_dbt_project(tmp_path)
        staging = dbt_dir / "models" / "staging"
        gen = DbtModelGenerator(tmp_path)
        weird = "t\x07x\x7fy\x85z"
        text = gen.generate_sources_yml(
            _source(),
            "raw_payments",
            [{"name": weird, "columns": [{"name": weird, "description": weird, "tests": []}]}],
        )
        (staging / "sources_payments.yml").write_text(text)
        (staging / "stg_payments.yml").write_text(
            gen.generate_staging_schema_yml(
                _source(),
                [{"name": "stg_payments__x", "description": weird, "columns": [{"name": weird}]}],
            )
        )
        result = _run_dbt(dbt_dir, tmp_path, "parse")
        assert result.returncode == 0, result.stdout + result.stderr
        manifest = json.loads((dbt_dir / "target" / "manifest.json").read_text())
        assert weird in {s["name"] for s in manifest["sources"].values()}

    def test_fallback_keywords_cover_duckdb_keywords(self) -> None:
        from dango.utils.dbt_escaping import FALLBACK_KEYWORDS

        conn = duckdb.connect(":memory:")
        words = {
            str(r[0]).lower()
            for r in conn.execute(
                "SELECT keyword_name FROM duckdb_keywords() WHERE keyword_category <> 'unreserved'"
            ).fetchall()
        }
        assert words <= FALLBACK_KEYWORDS
