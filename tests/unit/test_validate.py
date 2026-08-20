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


@pytest.mark.unit
class TestScriptPaths:
    def _validator(self, tmp_path):
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir(parents=True)
        return ProjectValidator(tmp_path), scripts_dir

    def test_clean_script_no_warnings(self, tmp_path):
        """Script using DANGO_PROJECT_ROOT env var should not warn."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "clean.py").write_text(
            'import os\nproject_root = os.environ["DANGO_PROJECT_ROOT"]\n'
        )

        v._check_script_paths()

        assert not any(r.name.startswith("scripts:") for r in v.results)

    def test_flags_path_home(self, tmp_path):
        """Script using Path.home() should warn."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "home.py").write_text("from pathlib import Path\ndir = Path.home()\n")

        v._check_script_paths()

        assert any(r.status == "warn" and "home.py" in r.name for r in v.results)

    def test_flags_hardcoded_users_path(self, tmp_path):
        """Script with hardcoded /Users/... path should warn."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "local.py").write_text("path = '/Users/aaronteoh/project/data.csv'\n")

        v._check_script_paths()

        assert any(r.status == "warn" and "local.py" in r.name for r in v.results)

    def test_flags_hardcoded_home_path(self, tmp_path):
        """Script with hardcoded /home/... path should warn."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "linux.py").write_text("path = '/home/user/project/data.csv'\n")

        v._check_script_paths()

        assert any(r.status == "warn" and "linux.py" in r.name for r in v.results)

    def test_flags_path_absolute_call(self, tmp_path):
        """Script with Path('/absolute/path') should warn."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "pathabs.py").write_text(
            'from pathlib import Path\ndir = Path("/home/data")\n'
        )

        v._check_script_paths()

        assert any(r.status == "warn" and "pathabs.py" in r.name for r in v.results)

    def test_no_scripts_dir(self, tmp_path):
        """No scripts/ dir should produce no results."""
        v = ProjectValidator(tmp_path)

        v._check_script_paths()

        assert not any(r.name.startswith("scripts:") for r in v.results)

    def test_skips_init_file(self, tmp_path):
        """__init__.py should be skipped."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "__init__.py").write_text("path = '/Users/foo/bar'\n")

        v._check_script_paths()

        assert not any(r.name.startswith("scripts:") for r in v.results)

    def test_skips_dotfiles(self, tmp_path):
        """Dotfiles should be skipped."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / ".hidden.py").write_text("path = '/Users/foo/bar'\n")

        v._check_script_paths()

        assert not any(r.name.startswith("scripts:") for r in v.results)

    def test_skips_underscore_prefix(self, tmp_path):
        """Files starting with _ should be skipped."""
        v, scripts_dir = self._validator(tmp_path)
        (scripts_dir / "_private.py").write_text("path = '/Users/foo/bar'\n")

        v._check_script_paths()

        assert not any(r.name.startswith("scripts:") for r in v.results)
