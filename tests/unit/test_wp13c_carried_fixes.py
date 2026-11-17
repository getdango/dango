"""tests/unit/test_wp13c_carried_fixes.py

Requirements append keeps its own line; malformed TOML names the file; compose text output is UTF-8.
"""

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.config.credentials import CredentialManager
from dango.exceptions import ConfigError
from dango.ingestion.dlt_runner import DltPipelineRunner
from dango.platform import compose_exec

pytestmark = pytest.mark.unit


def _sync_with_missing_dep(tmp_path: Path, existing: bytes | None) -> bytes:
    req = tmp_path / "requirements.txt"
    if existing is not None:
        req.write_bytes(existing)
    runner = DltPipelineRunner(tmp_path)
    source: Any = SimpleNamespace(
        name="s", type=SimpleNamespace(value="x"), empty_sync_policy="block"
    )
    with (
        patch.object(runner, "_check_dependencies", return_value=(False, [{"pip": "new-pkg"}])),
        patch(
            "dango.ingestion.dlt_runner.subprocess.check_call",
            side_effect=subprocess.CalledProcessError(1, "pip"),
        ),
        patch("dango.utils.activity_log.log_activity"),
    ):
        result = runner.run_source(source)
    assert result["status"] == "failed"
    return req.read_bytes()


def test_requirements_append_without_trailing_newline_keeps_lines_apart(tmp_path: Path) -> None:
    assert _sync_with_missing_dep(tmp_path, b"requests") == b"requests\nnew-pkg\n"


def test_requirements_append_to_terminated_file_adds_no_blank_line(tmp_path: Path) -> None:
    assert _sync_with_missing_dep(tmp_path, b"requests\n") == b"requests\nnew-pkg\n"


def test_requirements_created_when_absent(tmp_path: Path) -> None:
    assert _sync_with_missing_dep(tmp_path, None) == b"new-pkg\n"


def test_requirements_append_uses_lf_newlines(tmp_path: Path) -> None:
    out = _sync_with_missing_dep(tmp_path, b"requests\r\n")
    assert out.endswith(b"new-pkg\n") and b"\r\r" not in out


@pytest.mark.parametrize("name", ["secrets.toml", "config.toml"])
def test_malformed_toml_raising_index_error_is_a_config_error(tmp_path: Path, name: str) -> None:
    (tmp_path / ".dlt").mkdir()
    path = tmp_path / ".dlt" / name
    path.write_bytes(b"arr = [1,{= 2]\n")
    manager = CredentialManager(tmp_path)
    load = manager.load_secrets if name == "secrets.toml" else manager.load_config
    with pytest.raises(ConfigError, match="not valid TOML"):
        load()
    assert path.read_bytes() == b"arr = [1,{= 2]\n"


def test_local_compose_text_output_is_decoded_as_utf8(tmp_path: Path) -> None:
    done = subprocess.CompletedProcess([], 0, "", "")
    with patch("dango.platform.compose_exec.subprocess.run", return_value=done) as run:
        compose_exec.run_compose(tmp_path, "ps", timeout=5, text=True, env={})
    kwargs: dict[str, Any] = run.call_args.kwargs
    assert kwargs["encoding"] == "utf-8" and kwargs["errors"] == "replace"


def test_local_compose_bytes_output_stays_binary(tmp_path: Path) -> None:
    done = subprocess.CompletedProcess([], 0, b"", b"")
    with patch("dango.platform.compose_exec.subprocess.run", return_value=done) as run:
        compose_exec.run_compose(tmp_path, "ps", timeout=5, env={})
    assert "encoding" not in run.call_args.kwargs and "text" not in run.call_args.kwargs


def test_helper_compose_text_output_is_decoded_as_utf8(tmp_path: Path) -> None:
    proc = MagicMock()
    proc.communicate.return_value = ("", "")
    proc.returncode = 0
    with (
        patch("dango.platform.compose_exec.uses_helper", return_value=True),
        patch("dango.platform.compose_exec.subprocess.Popen", return_value=proc) as popen,
    ):
        compose_exec.run_compose(tmp_path, "ps", timeout=5, text=True)
    assert popen.call_args.kwargs["encoding"] == "utf-8"
    assert popen.call_args.kwargs["errors"] == "replace"
