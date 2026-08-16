"""tests/unit/test_validate.py

Tests for dango.cli.validate.ProjectValidator._check_model_sql_portability.
"""

from __future__ import annotations

import pytest

from dango.cli.validate import ProjectValidator


@pytest.mark.unit
class TestModelSqlPortability:
    def _validator(self, tmp_path):
        models_dir = tmp_path / "dbt" / "models" / "marts"
        models_dir.mkdir(parents=True)
        return ProjectValidator(tmp_path), models_dir

    def test_flags_read_csv_auto_absolute_path(self, tmp_path):
        v, models_dir = self._validator(tmp_path)
        (models_dir / "hard.sql").write_text(
            "SELECT *\nFROM read_csv_auto('/Users/foo/data.csv')\n"
        )

        v._check_model_sql_portability()

        assert any(r.status == "warn" and "hard.sql" in r.message for r in v.results)

    def test_flags_read_json(self, tmp_path):
        v, models_dir = self._validator(tmp_path)
        (models_dir / "j.sql").write_text("SELECT * FROM read_json('/srv/data.json')\n")

        v._check_model_sql_portability()

        assert any(r.status == "warn" and "j.sql" in r.message for r in v.results)

    def test_flags_windows_absolute_path(self, tmp_path):
        v, models_dir = self._validator(tmp_path)
        (models_dir / "win.sql").write_text("COPY t TO 'C:\\Users\\foo\\data.csv'\n")

        v._check_model_sql_portability()

        assert any(r.status == "warn" and "win.sql" in r.message for r in v.results)

    def test_ignores_comment_guidance(self, tmp_path):
        v, models_dir = self._validator(tmp_path)
        (models_dir / "guide.sql").write_text(
            "-- (Do NOT use read_csv_auto('/absolute/path') — it breaks)\nSELECT 1\n"
        )

        v._check_model_sql_portability()

        assert v.results == []

    def test_ignores_relative_ref(self, tmp_path):
        v, models_dir = self._validator(tmp_path)
        (models_dir / "ok.sql").write_text("SELECT * FROM {{ ref('stg_foo') }}\n")

        v._check_model_sql_portability()

        assert v.results == []
