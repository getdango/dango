"""tests/unit/test_check_wheel_imports.py

scripts/check_wheel_imports.py imports every module of an installed package and reports failures.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_wheel_imports.py"


def _run(package: str, extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = None
    if extra_path is not None:
        env = {**os.environ, "PYTHONPATH": str(extra_path)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--package", package],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def test_passes_for_importable_package(tmp_path: Path) -> None:
    pkg = tmp_path / "goodpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "sub.py").write_text("import json\n")
    result = _run("goodpkg", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failures" in result.stdout


def test_fails_and_names_module_that_cannot_import(tmp_path: Path) -> None:
    pkg = tmp_path / "badpkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "broken.py").write_text("import module_that_does_not_exist_xyz\n")
    result = _run("badpkg", tmp_path)
    assert result.returncode == 1
    assert "badpkg.broken" in result.stdout
    assert "module_that_does_not_exist_xyz" in result.stdout


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_wheel_imports", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _classify(name: str, exc: BaseException, platform: str = "win32") -> str:
    script = _load_script()
    return script.classify(
        name, exc, ["dango.ingestion.dlt_sources."], ["dango.runtime."], platform=platform
    )


@pytest.mark.parametrize(
    "exc",
    [
        ImportError("No module named 'fcntl'"),
        ModuleNotFoundError("No module named 'resource'"),
        ImportError("cannot import name 'O_DIRECTORY' from 'os'"),
        AttributeError("module 'os' has no attribute 'O_DIRECTORY'"),
    ],
)
def test_server_only_unix_import_error_is_expected(exc: BaseException) -> None:
    assert _classify("dango.runtime.isolated_run", exc) == "server_only"


def test_server_only_unrelated_import_error_still_fails() -> None:
    assert _classify("dango.runtime.isolated_run", ImportError("No module named 'yaml'")) == "fail"


def test_unix_only_error_outside_server_prefix_still_fails() -> None:
    assert _classify("dango.utils.dbt_lock", ImportError("No module named 'fcntl'")) == "fail"


def test_dlt_sources_missing_dependency_is_skipped() -> None:
    exc = ModuleNotFoundError("No module named 'pyairtable'")
    assert _classify("dango.ingestion.dlt_sources.airtable", exc) == "skip"


def test_dlt_sources_missing_dependency_exception_is_skipped() -> None:
    exc = type("MissingDependencyException", (Exception,), {})("needs extra")
    assert _classify("dango.ingestion.dlt_sources.airtable", exc) == "skip"


def test_dlt_sources_real_import_error_still_fails() -> None:
    exc = ImportError("cannot import name 'thing' from 'dango.ingestion'")
    assert _classify("dango.ingestion.dlt_sources.airtable", exc) == "fail"


def test_other_module_failure_fails() -> None:
    assert _classify("dango.cli.main", ModuleNotFoundError("No module named 'x'")) == "fail"


def _run_with(package: str, tmp_path: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--package", package, *flags],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


def _package(tmp_path: Path, name: str, module: str, body: str) -> None:
    pkg = tmp_path / name
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / f"{module}.py").write_text(body)


def test_run_lists_server_only_unix_failure_without_failing(tmp_path: Path) -> None:
    _package(tmp_path, "srvpkg", "svc", "raise ImportError(\"No module named 'fcntl'\")\n")
    result = _run_with("srvpkg", tmp_path, "--server-only-prefix", "srvpkg.svc")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "srvpkg.svc" in result.stdout
    assert "0 failures" in result.stdout


def test_run_fails_on_unrelated_error_in_server_only_prefix(tmp_path: Path) -> None:
    _package(tmp_path, "srvpkg2", "svc", "import module_that_does_not_exist_xyz\n")
    result = _run_with("srvpkg2", tmp_path, "--server-only-prefix", "srvpkg2.svc")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "srvpkg2.svc" in result.stdout


def test_run_fails_on_real_import_error_under_optional_prefix(tmp_path: Path) -> None:
    _package(tmp_path, "optpkg", "src", "from json import not_a_real_name\n")
    result = _run_with("optpkg", tmp_path, "--optional-prefix", "optpkg.src")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "optpkg.src" in result.stdout


def test_run_skips_missing_dependency_under_optional_prefix(tmp_path: Path) -> None:
    _package(tmp_path, "optpkg2", "src", "import module_that_does_not_exist_xyz\n")
    result = _run_with("optpkg2", tmp_path, "--optional-prefix", "optpkg2.src")
    assert result.returncode == 0, result.stdout + result.stderr


def test_dlt_sources_missing_project_module_still_fails() -> None:
    exc = ModuleNotFoundError(
        "No module named 'dango.ingestion.helpers'", name="dango.ingestion.helpers"
    )
    assert _classify("dango.ingestion.dlt_sources.airtable", exc) == "fail"


def test_server_only_class_never_applies_on_linux() -> None:
    exc = ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
    assert _classify("dango.runtime.isolated_run", exc, platform="linux") == "fail"


@pytest.mark.parametrize(
    "exc",
    [
        ImportError("cannot import name 'getuid' from 'dango.platform.cloud.helpers'"),
        ImportError("cannot import name 'chown' from 'dango.runtime.fs'"),
        AttributeError("'Settings' object has no attribute 'chown'"),
        ImportError("cannot import name 'fchmod' from 'somepkg'"),
    ],
)
def test_server_only_quoted_name_of_project_symbol_still_fails(exc: BaseException) -> None:
    assert _classify("dango.runtime.isolated_run", exc) == "fail"


def test_server_only_name_must_be_quoted_whole() -> None:
    exc = ModuleNotFoundError("No module named 'resourcepool'", name="resourcepool")
    assert _classify("dango.runtime.isolated_run", exc) == "fail"


def test_server_only_prefix_is_not_a_plain_string_prefix() -> None:
    exc = ModuleNotFoundError("No module named 'fcntl'", name="fcntl")
    assert _classify("dango.runtimex", exc) == "fail"
    assert _classify("dango.platform.cloudy.x", exc) == "fail"
    assert _classify("dango.runtime", exc) == "server_only"  # the package itself is server-only


def test_optional_prefix_does_not_match_the_package_itself() -> None:
    exc = ModuleNotFoundError("No module named 'pyairtable'")
    assert _classify("dango.ingestion.dlt_sources", exc) == "fail"


def _fake_dango(tmp_path: Path) -> None:
    root = tmp_path / "dango"
    (root / "platform" / "cloud").mkdir(parents=True)
    for pkg in (root, root / "platform", root / "platform" / "cloud"):
        (pkg / "__init__.py").write_text("")
    (root / "platform" / "cloud" / "svc.py").write_text(
        "raise ImportError(\"No module named 'fcntl'\")\n"
    )


def test_main_applies_default_server_only_prefixes_for_dango(tmp_path: Path) -> None:
    _fake_dango(tmp_path)
    result = _run_with("dango", tmp_path, "--platform", "win32")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dango.platform.cloud.svc" in result.stdout
    assert "1 expected server-only" in result.stdout


def test_main_treats_server_only_failure_as_failure_on_linux(tmp_path: Path) -> None:
    _fake_dango(tmp_path)
    result = _run_with("dango", tmp_path, "--platform", "linux")
    assert result.returncode == 1, result.stdout + result.stderr
