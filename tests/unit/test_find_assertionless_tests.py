"""tests/unit/test_find_assertionless_tests.py

scripts/find_assertionless_tests.py lists test functions that contain no assertion of any kind.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "find_assertionless_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("find_assertionless_tests", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SAMPLE = """
import pytest
from unittest import TestCase


def test_bare():
    x = 1


def test_assert_stmt():
    assert 1 == 1


def test_raises_ctx():
    with pytest.raises(ValueError):
        int("x")


def test_warns_ctx():
    with pytest.warns(UserWarning):
        pass


def test_mock_assert(mock):
    mock.assert_called_once_with(1)


def test_nested_mock_assert(mock):
    mock.a.b.assert_not_called()


def test_awaited(mock):
    mock.assert_awaited()


def test_helper_assert():
    _assert_ok()


def helper_no_assert():
    pass


@pytest.fixture
def fixture_no_assert():
    pass


class TestThing(TestCase):
    def test_self_assert(self):
        self.assertEqual(1, 1)

    def test_method_bare(self):
        pass

    def helper(self):
        pass


class TestPlain:
    def test_plain_bare(self):
        pass

    def test_plain_assert(self):
        assert True


async def test_async_bare():
    pass
"""


def test_finds_only_bare_tests(tmp_path):
    mod = _load()
    f = tmp_path / "test_sample.py"
    f.write_text(SAMPLE)
    hits = mod.find_in_file(f)
    assert hits == [
        "test_bare",
        "TestThing.test_method_bare",
        "TestPlain.test_plain_bare",
        "test_async_bare",
    ]


def test_cli_prints_path_double_colon_name_and_counts(tmp_path):
    (tmp_path / "test_sample.py").write_text(SAMPLE)
    (tmp_path / "notatest.py").write_text("def test_x():\n    pass\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = result.stdout.strip().splitlines()
    assert any(line.endswith("test_sample.py::test_bare") for line in lines)
    assert not any("notatest" in line for line in lines)
    assert result.returncode == 0


def test_cli_fail_on_hits_returns_nonzero(tmp_path):
    (tmp_path / "test_sample.py").write_text(SAMPLE)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--fail-on-hits", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1


def test_cli_clean_dir_returns_zero_with_fail_flag(tmp_path):
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--fail-on-hits", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
