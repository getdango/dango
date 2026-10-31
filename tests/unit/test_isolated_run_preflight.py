"""tests/unit/test_isolated_run_preflight.py

The runner entry point refuses to run user code while a denied path is readable.
Every check runs in a REAL subprocess with an empty environment.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.runtime import isolated_run

pytestmark = pytest.mark.unit

SCRIPT = Path(isolated_run.__file__)


def _project(tmp_path: Path, *, readable: bool) -> Path:
    root = tmp_path / "project"
    (root / ".dango").mkdir(parents=True)
    (root / ".dlt").mkdir()
    (root / "data").mkdir()
    (root / "scripts").mkdir()
    (root / ".env").write_text("SECRET=1\n")
    (root / "scripts" / "hello.py").write_text(
        "import os, sys\n"
        "print('umask', oct(os.umask(0o077)))\n"
        "print('path0', sys.path[0])\n"
        "print('argv', sys.argv[1:])\n"
        "print('secret', os.environ.get('SECRET'))\n"
    )
    (root / ".env").chmod(0o644 if readable else 0o000)
    for name in (".dango", ".dlt"):
        (root / name).chmod(0o755 if readable else 0o000)
    return root


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args],
        env={},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def cleanup(tmp_path: Path) -> Iterator[None]:
    yield
    root = tmp_path / "project"
    for name in (".env", ".dango", ".dlt"):
        if (root / name).exists():
            (root / name).chmod(0o755)


class TestPreflight:
    def test_readable_env_is_refused(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=True)
        result = _run(root, "--role", "script", "--cwd", str(root), "--check-only")
        assert result.returncode == 70
        assert "runner isolation is broken" in result.stderr
        assert ".env" in result.stderr

    def test_readable_env_alone_is_refused(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        (root / ".env").chmod(0o600)
        result = _run(root, "--role", "script", "--cwd", str(root), "--check-only")
        assert result.returncode == 70

    def test_readable_dango_dir_is_refused(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        (root / ".dango").chmod(0o755)
        result = _run(root, "--role", "script", "--cwd", str(root), "--check-only")
        assert result.returncode == 70
        assert ".dango" in result.stderr

    def test_unreadable_passes(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        result = _run(root, "--role", "script", "--cwd", str(root), "--check-only")
        assert result.returncode == 0, result.stderr

    def test_missing_paths_pass(self, tmp_path: Path) -> None:
        root = tmp_path / "bare"
        root.mkdir()
        result = _run(root, "--role", "script", "--cwd", str(root), "--check-only")
        assert result.returncode == 0, result.stderr

    def test_nb_role_also_denies_data(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        script = _run(root, "--role", "script", "--cwd", str(root), "--check-only")
        nb = _run(root, "--role", "nb", "--cwd", str(root), "--check-only")
        assert script.returncode == 0
        assert nb.returncode == 70
        assert "data" in nb.stderr
        (root / "data").chmod(0o000)
        try:
            assert _run(root, "--role", "nb", "--cwd", str(root), "--check-only").returncode == 0
        finally:
            (root / "data").chmod(0o755)


class TestExec:
    def test_runs_script_after_preflight(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        result = _run(
            root, "--role", "script", "--cwd", str(root), "--", "scripts/hello.py", "a", "b"
        )
        assert result.returncode == 0, result.stderr
        assert f"path0 {(root / 'scripts').resolve()}" in result.stdout
        assert "argv ['a', 'b']" in result.stdout
        assert "secret None" in result.stdout

    def test_umask_is_077(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        (root / "scripts" / "m.py").write_text("import os\nm = os.umask(0)\nprint(oct(m))\n")
        result = _run(root, "--role", "script", "--cwd", str(root), "--", "scripts/m.py")
        assert result.stdout.strip() == "0o77"

    def test_refuses_when_readable_even_with_target(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=True)
        result = _run(root, "--role", "script", "--cwd", str(root), "--", "scripts/hello.py")
        assert result.returncode == 70
        assert "path0" not in result.stdout

    def test_script_outside_scripts_dir_refused(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        (root / "evil.py").write_text("print('ran')\n")
        result = _run(root, "--role", "script", "--cwd", str(root), "--", "evil.py")
        assert result.returncode == 64
        assert "ran" not in result.stdout

    def test_symlink_escape_refused(self, tmp_path: Path, cleanup: None) -> None:
        root = _project(tmp_path, readable=False)
        (root / "evil.py").write_text("print('ran')\n")
        (root / "scripts" / "link.py").symlink_to(root / "evil.py")
        result = _run(root, "--role", "script", "--cwd", str(root), "--", "scripts/link.py")
        assert result.returncode == 64

    def test_usage_errors(self, tmp_path: Path) -> None:
        assert _run(tmp_path, "--role", "bogus", "--cwd", str(tmp_path)).returncode == 64
        assert _run(tmp_path, "--role", "script", "--cwd", str(tmp_path)).returncode == 64


class TestStdlibOnly:
    def test_imports_only_stdlib(self) -> None:
        tree = ast.parse(SCRIPT.read_text())
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, "relative import"
                modules.add((node.module or "").split(".")[0])
        assert modules
        assert modules <= set(sys.stdlib_module_names), modules - set(sys.stdlib_module_names)
        assert "dango" not in modules
