"""tests/unit/test_validate_script_env.py

`dango validate` warns (never fails) when a script reads an environment variable
it does not declare in `.dango/scripts.yml`, when a declared name is missing from
`.env`, and when a script appears to write the warehouse without `warehouse: write`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.cli.validate import ProjectValidator


def _check(root: Path) -> list[tuple[str, str, str]]:
    from dango.cli.validate_script_env import check_script_env

    return list(check_script_env(root))


def _script(root: Path, rel: str, body: str) -> None:
    path = root / "scripts" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _declare(root: Path, text: str) -> None:
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "scripts.yml").write_text(text, encoding="utf-8")


def _warns(results: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    return [r for r in results if r[1] == "warn"]


@pytest.mark.unit
class TestUndeclaredEnv:
    @pytest.mark.parametrize(
        "body",
        [
            'import os\nx = os.environ["SLACK_TOKEN"]\n',
            'import os\nx = os.environ.get("SLACK_TOKEN")\n',
            'import os\nx = os.getenv("SLACK_TOKEN", "d")\n',
            'from os import environ\nx = environ["SLACK_TOKEN"]\n',
            'from os import environ\nx = environ.get("SLACK_TOKEN")\n',
            'from os import getenv\nx = getenv("SLACK_TOKEN")\n',
            'import os as o\nx = o.environ["SLACK_TOKEN"]\n',
        ],
    )
    def test_undeclared_read_warns(self, tmp_path, body):
        _script(tmp_path, "a.py", body)

        warns = _warns(_check(tmp_path))

        assert len(warns) == 1
        assert warns[0][0] == "scripts: a.py"
        assert "reads SLACK_TOKEN but does not declare it" in warns[0][2]
        assert "will not receive it" in warns[0][2]

    def test_nested_script_uses_relative_path(self, tmp_path):
        _script(tmp_path, "reports/x.py", 'import os\nos.getenv("TOK")\n')
        _declare(tmp_path, "scripts:\n  - path: reports/x.py\n    env: [TOK]\n")
        (tmp_path / ".env").write_text("TOK=1\n")

        assert _warns(_check(tmp_path)) == []

    def test_declared_name_is_silent(self, tmp_path):
        _script(tmp_path, "a.py", 'import os\nx = os.environ["SLACK_TOKEN"]\n')
        _declare(tmp_path, "scripts:\n  - path: a.py\n    env: [SLACK_TOKEN]\n")
        (tmp_path / ".env").write_text("SLACK_TOKEN=abc\n")

        assert _warns(_check(tmp_path)) == []

    def test_declaration_for_other_script_does_not_cover(self, tmp_path):
        _script(tmp_path, "a.py", 'import os\nx = os.environ["SLACK_TOKEN"]\n')
        _declare(tmp_path, "scripts:\n  - path: b.py\n    env: [SLACK_TOKEN]\n")

        assert any("a.py" in r[0] for r in _warns(_check(tmp_path)))

    @pytest.mark.parametrize(
        "name",
        ["PATH", "HOME", "LANG", "LC_ALL", "TZ", "DANGO_PROJECT_ROOT", "DANGO_WORKSPACE"],
    )
    def test_always_provided_names_are_silent(self, tmp_path, name):
        _script(tmp_path, "a.py", f'import os\nx = os.environ["{name}"]\n')

        assert _warns(_check(tmp_path)) == []

    def test_dynamic_names_not_flagged(self, tmp_path):
        _script(
            tmp_path,
            "a.py",
            "import os\nvar = 'X'\na = os.environ[var]\nb = os.getenv(var)\nc = os.environ.get(var)\n",
        )

        assert _warns(_check(tmp_path)) == []

    def test_assignment_to_environ_is_not_a_read(self, tmp_path):
        _script(tmp_path, "a.py", 'import os\nos.environ["MINE"] = "1"\n')

        assert _warns(_check(tmp_path)) == []

    def test_private_and_init_files_ignored(self, tmp_path):
        _script(tmp_path, "_helper.py", 'import os\nos.getenv("X1")\n')
        _script(tmp_path, "__init__.py", 'import os\nos.getenv("X2")\n')

        assert _check(tmp_path) == []

    def test_unreadable_scripts_yml_does_not_crash(self, tmp_path):
        _script(tmp_path, "a.py", 'import os\nos.getenv("X")\n')
        _declare(tmp_path, "scripts: [not valid\n")

        results = _check(tmp_path)

        assert all(r[1] != "fail" for r in results)


@pytest.mark.unit
class TestDeclaredButMissing:
    def test_declared_name_missing_from_dotenv_warns(self, tmp_path):
        _script(tmp_path, "a.py", "pass\n")
        _declare(tmp_path, "scripts:\n  - path: a.py\n    env: [SLACK_TOKEN]\n")
        (tmp_path / ".env").write_text("OTHER=1\n")

        warns = _warns(_check(tmp_path))

        assert len(warns) == 1
        assert "SLACK_TOKEN" in warns[0][2]
        assert ".env" in warns[0][2]

    def test_declared_name_missing_when_no_dotenv_file(self, tmp_path):
        _script(tmp_path, "a.py", "pass\n")
        _declare(tmp_path, "scripts:\n  - path: a.py\n    env: [SLACK_TOKEN]\n")

        assert len(_warns(_check(tmp_path))) == 1

    def test_declared_name_present_is_silent(self, tmp_path):
        _script(tmp_path, "a.py", "pass\n")
        _declare(tmp_path, "scripts:\n  - path: a.py\n    env: [SLACK_TOKEN]\n")
        (tmp_path / ".env").write_text("SLACK_TOKEN=abc\n")

        assert _warns(_check(tmp_path)) == []


@pytest.mark.unit
class TestWarehouseWrite:
    @pytest.mark.parametrize(
        "body",
        [
            "import duckdb\nc = duckdb.connect('data/warehouse.duckdb')\n"
            "c.execute('INSERT INTO raw.t VALUES (1)')\n",
            "import duckdb\nc = duckdb.connect('data/warehouse.duckdb', read_only=False)\n"
            "c.execute('create or replace table raw.t as select 1')\n",
            "import duckdb\nc = duckdb.connect('data/warehouse.duckdb')\n"
            "c.execute(\"COPY raw.t TO 'out.parquet'\")\n",
            "from duckdb import connect\nc = connect('data/warehouse.duckdb')\n"
            "c.execute('DELETE FROM raw.t')\n",
            "import duckdb\nfrom dango.utils.dbt_lock import DbtLock\n"
            "with DbtLock():\n    c = duckdb.connect('data/warehouse.duckdb')\n",
        ],
    )
    def test_apparent_write_without_declaration_warns(self, tmp_path, body):
        _script(tmp_path, "w.py", body)

        warns = _warns(_check(tmp_path))

        assert len(warns) == 1
        assert warns[0][0] == "scripts: w.py"
        assert "warehouse: write" in warns[0][2]

    def test_declared_write_is_silent(self, tmp_path):
        _script(
            tmp_path,
            "w.py",
            "import duckdb\nc = duckdb.connect('data/warehouse.duckdb')\nc.execute('INSERT INTO t VALUES (1)')\n",
        )
        _declare(tmp_path, "scripts:\n  - path: w.py\n    warehouse: write\n")

        assert _warns(_check(tmp_path)) == []

    def test_read_only_connection_is_silent(self, tmp_path):
        _script(
            tmp_path,
            "r.py",
            "import duckdb\nc = duckdb.connect('data/warehouse.duckdb', read_only=True)\n"
            "c.execute('select 1')\n",
        )

        assert _warns(_check(tmp_path)) == []

    def test_write_sql_without_connect_is_silent(self, tmp_path):
        _script(tmp_path, "s.py", "q = 'INSERT INTO t VALUES (1)'\n")

        assert _warns(_check(tmp_path)) == []

    def test_in_memory_connection_is_silent(self, tmp_path):
        _script(
            tmp_path,
            "m.py",
            "import duckdb\nc = duckdb.connect(':memory:')\nc.execute('create table t (i int)')\n",
        )

        assert _warns(_check(tmp_path)) == []

    def test_connect_without_write_is_silent(self, tmp_path):
        _script(
            tmp_path,
            "r.py",
            "import duckdb\nc = duckdb.connect('data/warehouse.duckdb')\nc.execute('select 1')\n",
        )

        assert _warns(_check(tmp_path)) == []


@pytest.mark.unit
class TestRobustness:
    def test_syntax_error_script_is_ignored_with_info(self, tmp_path):
        _script(tmp_path, "bad.py", "def (:\n")
        _script(tmp_path, "good.py", 'import os\nos.getenv("X")\n')

        results = _check(tmp_path)

        bad = [r for r in results if "bad.py" in r[0]]
        assert len(bad) == 1
        assert bad[0][1] == "pass"
        assert "syntax error" in bad[0][2]
        assert any("good.py" in r[0] for r in _warns(results))

    def test_never_returns_fail(self, tmp_path):
        _script(tmp_path, "bad.py", "def (:\n")
        _script(tmp_path, "a.py", 'import os\nos.getenv("X")\n')

        assert all(r[1] in {"pass", "warn"} for r in _check(tmp_path))

    def test_no_scripts_dir_yields_nothing(self, tmp_path):
        assert _check(tmp_path) == []

    def test_validator_output_unchanged_without_scripts(self, tmp_path):
        v = ProjectValidator(tmp_path)

        v.validate_all(display=False, include_connectivity=False)

        assert not [r for r in v.results if r.name.startswith("scripts")]

    def test_validator_reports_script_warning(self, tmp_path):
        _script(tmp_path, "a.py", 'import os\nos.getenv("SLACK_TOKEN")\n')
        v = ProjectValidator(tmp_path)

        v.validate_all(display=False, include_connectivity=False)

        found = [r for r in v.results if r.name == "scripts: a.py" and r.status == "warn"]
        assert len(found) == 1
