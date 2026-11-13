"""tests/unit/test_scripts_config_env.py

``env:`` and ``warehouse:`` on a script entry in ``.dango/scripts.yml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dango.config.exceptions import ConfigValidationError
from dango.config.scripts import ScriptConfig, ScriptsConfig, load_scripts_config

pytestmark = pytest.mark.unit


def _write(root: Path, text: str) -> None:
    (root / ".dango").mkdir(exist_ok=True)
    (root / ".dango" / "scripts.yml").write_text(text)


class TestParse:
    def test_env_and_warehouse(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "scripts:\n"
            "  - path: reports/a.py\n"
            "    timeout_seconds: 60\n"
            "    env: [SLACK_WEBHOOK_URL, SHEET_NAME]\n"
            "    warehouse: write\n",
        )
        cfg = load_scripts_config(tmp_path)
        assert cfg.env_for("reports/a.py") == ("SLACK_WEBHOOK_URL", "SHEET_NAME")
        assert cfg.warehouse_for("reports/a.py") == "write"
        assert cfg.timeout_for("reports/a.py") == 60

    def test_defaults(self) -> None:
        script = ScriptConfig(path="a.py")
        assert script.env == []
        assert script.warehouse == "read"

    def test_unlisted_script(self) -> None:
        cfg = ScriptsConfig(scripts=[ScriptConfig(path="a.py", env=["A_B"])])
        assert cfg.env_for("other.py") == ()
        assert cfg.warehouse_for("other.py") == "read"

    def test_duplicates_collapse_in_order(self) -> None:
        script = ScriptConfig(path="a.py", env=["B_X", "A_X", "B_X"])
        assert script.env == ["B_X", "A_X"]


class TestRejected:
    @pytest.mark.parametrize(
        "name",
        [
            "lower",
            "1ABC",
            "A-B",
            "A B",
            "",
            "A" * 129,
            "TRAILING\n",
            "DANGO_TOKEN",
            "PYTHONPATH",
            "PYTHON",
            "LD_PRELOAD",
            "DYLD_INSERT_LIBRARIES",
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "DLT_DATA_DIR",
        ],
    )
    def test_invalid_or_reserved_name(self, name: str) -> None:
        with pytest.raises(ValueError):
            ScriptConfig(path="a.py", env=[name])

    def test_bad_warehouse(self) -> None:
        with pytest.raises(ValueError):
            ScriptConfig.model_validate({"path": "a.py", "warehouse": "admin"})

    def test_loader_wraps_error(self, tmp_path: Path) -> None:
        _write(tmp_path, "scripts:\n  - path: a.py\n    env: [bad-name]\n")
        with pytest.raises(ConfigValidationError):
            load_scripts_config(tmp_path)


class TestBackwardCompatible:
    def test_old_file_loads(self, tmp_path: Path) -> None:
        _write(tmp_path, "scripts:\n  - path: a.py\n    timeout_seconds: 10\n  - path: b.py\n")
        cfg = load_scripts_config(tmp_path)
        assert cfg.timeout_for("a.py") == 10
        assert cfg.timeout_for("b.py") == 300
        assert cfg.env_for("a.py") == ()
        assert cfg.warehouse_for("b.py") == "read"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert load_scripts_config(tmp_path).scripts == []
