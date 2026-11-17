"""tests/unit/test_script_launch_encoding.py

User scripts are decoded as UTF-8, and every child environment tells Python to write UTF-8.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from dango.platform import run_as, script_launch
from tests.factories.cp1252_child import run_child

pytestmark = pytest.mark.unit

_SECRET = "declared-secret-value"


def _project(tmp_path: Path, scripts_yml: str = "") -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "s.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / ".dango").mkdir()
    (tmp_path / ".env").write_text(f"SLACK_URL={_SECRET}\n", encoding="utf-8")
    (tmp_path / ".dango" / "scripts.yml").write_text(
        scripts_yml or "scripts:\n  - path: s.py\n    env: [SLACK_URL]\n", encoding="utf-8"
    )
    return tmp_path


def _started(project: Path, monkeypatch: pytest.MonkeyPatch, isolation_on: bool) -> Any:
    monkeypatch.setattr(run_as, "uses_isolation", lambda: isolation_on)
    monkeypatch.setattr(run_as, "isolation_state", lambda: "on" if isolation_on else "off")
    with patch("subprocess.Popen", return_value=MagicMock()) as popen:
        script_launch.start_script(project, "s.py", project / "scripts" / "s.py")
    return popen.call_args


def _assert_decoding(call: Any) -> None:
    assert call.kwargs["text"] is True
    assert call.kwargs["encoding"] == "utf-8"
    assert call.kwargs["errors"] == "replace"
    assert call.kwargs["env"]["PYTHONIOENCODING"] == "utf-8"


def test_local_launch_decodes_utf8_and_sets_child_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    _assert_decoding(_started(_project(tmp_path), monkeypatch, isolation_on=False))


def test_warehouse_write_launch_decodes_utf8_and_sets_child_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yml = "scripts:\n  - path: s.py\n    env: [SLACK_URL]\n    warehouse: write\n"
    call = _started(_project(tmp_path, yml), monkeypatch, isolation_on=True)
    assert call.args[0][0] != run_as.SUDO
    _assert_decoding(call)


def test_runner_launch_decodes_utf8_and_passes_the_encoding_through_sudo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    call = _started(_project(tmp_path), monkeypatch, isolation_on=True)
    _assert_decoding(call)
    preserved = next(a for a in call.args[0] if a.startswith("--preserve-env="))
    assert "PYTHONIOENCODING" in preserved.split("=", 1)[1].split(",")


def test_build_env_declares_utf8_output() -> None:
    env = run_as.build_env("script", Path("/p"), {})
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert run_as.build_env("nb", Path("/p"), {})["PYTHONIOENCODING"] == "utf-8"


def test_script_output_roundtrips_under_a_non_utf8_parent(tmp_path: Path) -> None:
    result = run_child(
        """
        from dango.platform import run_as, script_launch
        run_as.require_isolation_ready = lambda: "off"
        (root / "scripts").mkdir()
        script = root / "scripts" / "s.py"
        script.write_text("import sys\\nprint('caf\\u00e9 \\u0141')\\nprint('err \\u0141', file=sys.stderr)\\n",
                          encoding="utf-8")
        started = script_launch.start_script(root, "s.py", script)
        out, err, timed_out = script_launch.communicate_with_timeout(started.proc, 60)
        result = {"out": ascii(out), "err": ascii(err), "timed_out": timed_out,
                  "code": started.proc.returncode}
        """,
        tmp_path,
    )
    assert result["code"] == 0
    assert result["out"] == ascii("café Ł\n")
    assert result["err"] == ascii("err Ł\n")
    assert result["timed_out"] is False
