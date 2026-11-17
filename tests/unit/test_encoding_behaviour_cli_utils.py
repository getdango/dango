"""tests/unit/test_encoding_behaviour_cli_utils.py

Representative CLI/config/utils writers produce UTF-8 bytes and LF newlines under a non-UTF-8 locale.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TEXT = "café — ✓ über"

# Runs inside the subprocess: records the ``newline`` argument of every text-mode write open
# so Windows newline translation (invisible on POSIX) is still observable.
_PRELUDE = """
import builtins, io, json, sys
from pathlib import Path

from dango.utils.stdio import ensure_safe_stdio

ensure_safe_stdio()
TEXT = __TEXT__
root = Path(sys.argv[1])
_writes = []
_real_open = io.open

def _spy(file, mode="r", buffering=-1, encoding=None, errors=None, newline=None, *a, **kw):
    if "b" not in mode and any(c in mode for c in "wax"):
        _writes.append(newline)
    return _real_open(file, mode, buffering, encoding, errors, newline, *a, **kw)

io.open = _spy
builtins.open = _spy
""".replace("__TEXT__", repr(TEXT))


def _run(body: str, tmp_path: Path) -> dict[str, Any]:
    """Run ``body`` in a cp1252-like (non-UTF-8) interpreter; return its JSON result."""
    script = _PRELUDE + textwrap.dedent(body) + "\nprint(json.dumps(result))\n"
    env = {
        **os.environ,
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "HOME": str(tmp_path / "home"),
        "USERPROFILE": str(tmp_path / "home"),
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "cp1252",
        "LC_ALL": "C",
        "DANGO_TELEMETRY": "0",
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(project)],
        env=env,
        capture_output=True,
        timeout=120,
        cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")[-1500:]
    result: dict[str, Any] = json.loads(proc.stdout.decode("ascii").strip().splitlines()[-1])
    return result


def _assert_lf(result: dict[str, Any]) -> None:
    assert result["writes"], "no text write was observed"
    assert set(result["writes"]) == {"\n"}, result["writes"]


def test_model_docs_yaml_roundtrips_non_ascii(tmp_path: Path) -> None:
    result = _run(
        """
        from dango.transformation import model_docs
        target = root / "schema.yml"
        model_docs._write_yaml(target, {"models": [{"name": "m", "description": TEXT}]})
        loaded = model_docs._load_yaml(target)
        result = {"raw": target.read_bytes().decode("utf-8"),
                  "same": loaded["models"][0]["description"] == TEXT,
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert TEXT in result["raw"]
    assert result["same"]
    _assert_lf(result)


def test_schema_manager_writes_utf8_yaml_with_lf(tmp_path: Path) -> None:
    result = _run(
        """
        from dango.cli.schema_manager import SchemaManager
        mgr = SchemaManager(root, root / "x.duckdb")
        target = root / "schema.yml"
        mgr._write_schema_yml(target, {"version": 2, "models": [{"name": "m", "description": TEXT}]})
        loaded = mgr._load_schema_yml(target)
        result = {"raw": target.read_bytes().decode("utf-8"),
                  "same": loaded["models"][0]["description"] == TEXT,
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert TEXT in result["raw"]
    assert result["same"]
    _assert_lf(result)


def test_oauth_storage_writes_utf8_toml_with_lf(tmp_path: Path) -> None:
    result = _run(
        """
        from dango.oauth.storage import OAuthStorage
        store = OAuthStorage(root)
        store._save_secrets({"sources": {"s": {"credentials": {"client_id": TEXT}}}})
        loaded = store._load_secrets()
        result = {"raw": store.secrets_file.read_bytes().decode("utf-8"),
                  "same": loaded["sources"]["s"]["credentials"]["client_id"] == TEXT,
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert TEXT in result["raw"]
    assert result["same"]
    _assert_lf(result)


def test_cli_oauth_env_file_is_utf8_with_lf(tmp_path: Path) -> None:
    result = _run(
        """
        from dango.cli.oauth import OAuthHelper
        helper = OAuthHelper(root)
        helper.save_to_env("APP_NAME", TEXT, comment=TEXT)
        result = {"raw": (root / ".env").read_bytes().decode("utf-8"),
                  "read": helper.read_from_env("APP_NAME") == TEXT,
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert TEXT in result["raw"]
    assert result["read"]
    _assert_lf(result)


def test_credential_manager_saves_utf8_toml_with_lf(tmp_path: Path) -> None:
    result = _run(
        """
        from dango.config.credentials import CredentialManager
        mgr = CredentialManager(root)
        mgr.save_secrets({"sources": {"s": {"key": TEXT}}})
        mgr.save_config({"runtime": {"note": TEXT}})
        result = {"secrets": mgr.secrets_file.read_bytes().decode("utf-8"),
                  "config": mgr.config_file.read_bytes().decode("utf-8"),
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert TEXT in result["secrets"]
    assert TEXT in result["config"]
    _assert_lf(result)


def test_gitignore_addition_keeps_utf8_and_uses_lf(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    (tmp_path / "proj" / ".gitignore").write_bytes(f"# {TEXT}\n*.pyc\n".encode())
    result = _run(
        """
        from dango.config.credentials import ensure_sensitive_artifact_gitignores
        changed = ensure_sensitive_artifact_gitignores(root)
        result = {"changed": changed,
                  "raw": (root / ".gitignore").read_bytes().decode("utf-8"),
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert result["changed"]
    assert TEXT in result["raw"]
    _assert_lf(result)


def test_model_ref_graph_reads_utf8_sql(tmp_path: Path) -> None:
    models = tmp_path / "proj" / "dbt" / "models" / "marts"
    models.mkdir(parents=True)
    (models / "orders.sql").write_bytes(
        f"-- {TEXT}\nselect * from {{{{ ref('stg_a') }}}}\n".encode()
    )
    result = _run(
        """
        from dango.transformation.model_sql import _find_cycle
        result = {"cycle": _find_cycle(root, "new_model", ["orders"]), "writes": _writes}
        """,
        tmp_path,
    )
    assert result["cycle"] is None


def test_dbt_status_reads_utf8_run_results(tmp_path: Path) -> None:
    target = tmp_path / "proj" / "dbt" / "target"
    target.mkdir(parents=True)
    payload = {"results": [{"unique_id": f"model.p.{TEXT}", "status": "success", "timing": []}]}
    (target / "run_results.json").write_bytes(json.dumps(payload, ensure_ascii=False).encode())
    result = _run(
        """
        from dango.utils.dbt_status import update_model_status
        update_model_status(root)
        saved = root / ".dango" / "dbt_model_status.json"
        result = {"saved": saved.exists() and "model.p." in saved.read_text(encoding="utf-8"),
                  "writes": _writes}
        """,
        tmp_path,
    )
    assert result["saved"]
