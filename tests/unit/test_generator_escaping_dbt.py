"""tests/unit/test_generator_escaping_dbt.py

Real dbt runs over generated files: quote:true on keyword columns, snapshot template
escaping, control characters and the fallback keyword list.
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
from dango.transformation.generator import DbtModelGenerator

CANARY_VALUE = "CANARY_SECRET_9f3a"


def _source() -> DataSource:
    return DataSource(name="payments", type=SourceType.STRIPE)


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
        dbt_dir = _write_dbt_project(tmp_path)
        (dbt_dir / "snapshots").mkdir()
        (dbt_dir / "snapshots" / "snap_t.sql").write_text(rendered)
        (dbt_dir / "models" / "staging" / "s.yml").write_text(_source_yml_for(tmp_path, hostile))
        result = _run_dbt(dbt_dir, tmp_path, "parse")
        assert result.returncode == 0, result.stdout + result.stderr
        assert CANARY_VALUE not in (dbt_dir / "target" / "manifest.json").read_text()


@pytest.mark.unit
def test_snapshot_tag_cannot_be_broken_out_of() -> None:
    from dango.cli.commands.snapshot import _render_snapshot

    rendered = _render_snapshot(
        generated_at="now",
        snapshot_name="snap_a %}{{ env_var('CANARY') }}{% snapshot b",
        source_name="payments",
        table_name="t",
        strategy="check",
        unique_key="id', x=env_var('CANARY'",
        updated_at="",
    )
    import jinja2
    from jinja2 import nodes

    assert re.search(r"\{% snapshot \w+ %\}", rendered)
    body = re.sub(r"\{% (end)?snapshot[^%]*%\}", "", rendered)
    tree = jinja2.Environment().parse(body)
    called = {c.node.name for c in tree.find_all(nodes.Call) if isinstance(c.node, nodes.Name)}
    assert "env_var" not in called


def _source_yml_for(tmp_path: Path, table: str) -> str:
    return DbtModelGenerator(tmp_path).generate_sources_yml(
        _source(), "raw_payments", [{"name": table, "columns": [{"name": "id", "tests": []}]}]
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
