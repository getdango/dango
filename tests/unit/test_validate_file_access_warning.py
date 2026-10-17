"""tests/unit/test_validate_file_access_warning.py

Tests that `dango validate` warns (never fails) on human-written models that read files or URLs.
"""

from __future__ import annotations

from typing import Any

import pytest

from dango.cli.validate import ProjectValidator
from dango.transformation.model_sql_guard import check_model_sql


def _project(tmp_path, files: dict[str, str]) -> ProjectValidator:
    for rel, sql in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sql)
    return ProjectValidator(tmp_path)


def _file_warnings(v: ProjectValidator) -> list:
    return [r for r in v.results if r.name == "dbt: Model file access"]


@pytest.mark.unit
class TestFileAccessWarning:
    def test_warns_on_read_csv_view(self, tmp_path):
        v = _project(
            tmp_path,
            {"dbt/models/marts/v.sql": "select * from read_csv('data/uploads/x.csv')\n"},
        )

        v._check_model_file_access()

        found = _file_warnings(v)
        assert len(found) == 1
        assert found[0].status == "warn"
        assert "dbt/models/marts/v.sql" in found[0].message
        assert "read_csv" in found[0].message
        assert "local_files" in found[0].message

    def test_warns_on_replacement_scan_literal(self, tmp_path):
        v = _project(
            tmp_path,
            {"dbt/models/marts/r.sql": "select * from 'data/uploads/x.csv'\n"},
        )

        v._check_model_file_access()

        found = _file_warnings(v)
        assert len(found) == 1
        assert "dbt/models/marts/r.sql" in found[0].message
        assert "x.csv" in found[0].message

    def test_warns_on_snapshot_directory(self, tmp_path):
        v = _project(
            tmp_path,
            {
                "dbt/snapshots/s.sql": "{% snapshot s %}\n{{ config(unique_key='id') }}\n"
                "select * from read_parquet('/tmp/x.parquet')\n{% endsnapshot %}\n"
            },
        )

        v._check_model_file_access()

        assert len(_file_warnings(v)) == 1

    def test_no_warning_for_plain_select(self, tmp_path):
        v = _project(
            tmp_path,
            {
                "dbt/models/marts/ok.sql": "select a, count(*) n from {{ ref('stg_x') }} group by a\n",
                "dbt/models/staging/s.sql": "select * from {{ source('raw', 't') }}\n",
            },
        )

        v._check_model_file_access()

        assert v.results == []

    def test_no_warning_for_custom_jinja_macro_model(self, tmp_path):
        v = _project(
            tmp_path,
            {
                "dbt/models/marts/m.sql": "{% set cols = ['a', 'b'] %}\n"
                "select {% for c in cols %}{{ c }}{% if not loop.last %},{% endif %}{% endfor %}\n"
                "from {{ ref('stg_x') }} where d > {{ my_macro('x') }}\n"
            },
        )

        v._check_model_file_access()

        assert v.results == []

    def test_unlisted_function_is_not_reported_as_file_access(self, tmp_path):
        v = _project(
            tmp_path,
            {"dbt/models/marts/f.sql": "select my_udf(a) from {{ ref('stg_x') }}\n"},
        )

        v._check_model_file_access()

        assert v.results == []

    def test_seeds_directory_not_scanned(self, tmp_path):
        v = _project(
            tmp_path,
            {"dbt/seeds/x.sql": "select * from read_csv('data/x.csv')\n"},
        )

        v._check_model_file_access()

        assert v.results == []

    def test_warning_not_error_exit_code_zero(self, tmp_path):
        v = _project(
            tmp_path,
            {"dbt/models/marts/v.sql": "select * from read_csv('data/x.csv')\n"},
        )

        v._check_model_file_access()
        summary = v._create_summary()

        assert summary["warn"] == 1
        assert summary["fail"] == 0
        assert summary["is_valid"] is True

    def test_registered_in_validate_all(self, tmp_path):
        v = _project(
            tmp_path,
            {"dbt/models/marts/v.sql": "select * from read_csv('data/x.csv')\n"},
        )

        v.validate_all(display=False, include_connectivity=False)

        assert len(_file_warnings(v)) == 1


@pytest.mark.unit
class TestJinjaModeParameter:
    def test_check_model_sql_default_is_strict(self):
        sql = "select * from {{ ref('a') }} where x > {{ my_macro() }}"

        assert check_model_sql(sql) == check_model_sql(sql, jinja="strict")
        assert any("Jinja construct not allowed" in e for e in check_model_sql(sql))

    def test_ignore_mode_skips_jinja_errors_but_still_flags_files(self):
        assert check_model_sql("select {{ my_macro() }} from {{ ref('a') }}", jinja="ignore") == []
        errors = check_model_sql("select {{ m() }} from read_csv('x.csv')", jinja="ignore")
        assert errors
        assert all("Jinja" not in e for e in errors)

    def test_invalid_mode_rejected(self):
        bad_mode: Any = "bogus"
        with pytest.raises(ValueError):
            check_model_sql("select 1", jinja=bad_mode)
