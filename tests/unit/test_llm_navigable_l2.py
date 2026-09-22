"""tests/unit/test_llm_navigable_l2.py

Tests for dango.cli.validate.ProjectValidator._check_description_completeness.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.unit
class TestDescriptionCompleteness:
    def _make_schema(self, tmp_path: Path, models: list[dict]) -> Path:
        schema_file = tmp_path / "dbt" / "models" / "staging" / "schema.yml"
        schema_file.parent.mkdir(parents=True, exist_ok=True)
        schema_file.write_text(yaml.dump({"version": 2, "models": models}))
        return tmp_path

    def test_no_issues_passes(self, tmp_path):
        from dango.cli.validate import ProjectValidator

        self._make_schema(
            tmp_path,
            [{"name": "stg_orders", "description": "Cleaned orders data", "columns": []}],
        )
        v = ProjectValidator(tmp_path)
        v._check_description_completeness()
        statuses = [r.status for r in v.results]
        assert "fail" not in statuses
        assert any(r.status == "pass" for r in v.results)

    def test_todo_model_description_warns(self, tmp_path):
        from dango.cli.validate import ProjectValidator

        self._make_schema(
            tmp_path,
            [{"name": "stg_orders", "description": "# TODO: Add description", "columns": []}],
        )
        v = ProjectValidator(tmp_path)
        v._check_description_completeness()
        statuses = [r.status for r in v.results]
        assert "warn" in statuses
        assert "fail" not in statuses

    def test_empty_description_warns(self, tmp_path):
        from dango.cli.validate import ProjectValidator

        self._make_schema(tmp_path, [{"name": "stg_orders", "description": "", "columns": []}])
        v = ProjectValidator(tmp_path)
        v._check_description_completeness()
        assert any(r.status == "warn" for r in v.results)

    def test_todo_column_warns(self, tmp_path):
        from dango.cli.validate import ProjectValidator

        self._make_schema(
            tmp_path,
            [
                {
                    "name": "stg_orders",
                    "description": "Good description",
                    "columns": [{"name": "order_id", "description": "TODO: Add description"}],
                }
            ],
        )
        v = ProjectValidator(tmp_path)
        v._check_description_completeness()
        assert any("column" in r.name.lower() and r.status == "warn" for r in v.results)

    def test_no_schema_files_warns(self, tmp_path):
        from dango.cli.validate import ProjectValidator

        models_dir = tmp_path / "dbt" / "models"
        models_dir.mkdir(parents=True)
        v = ProjectValidator(tmp_path)
        v._check_description_completeness()
        assert any(r.status == "warn" for r in v.results)

    def test_no_models_dir_returns_silently(self, tmp_path):
        from dango.cli.validate import ProjectValidator

        v = ProjectValidator(tmp_path)
        v._check_description_completeness()
        # Should not raise, result count may be 0
        assert isinstance(v.results, list)
