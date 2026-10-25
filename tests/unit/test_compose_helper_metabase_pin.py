"""tests/unit/test_compose_helper_metabase_pin.py

The compose helper's root-only pin-metabase verb: syntax, sudo refusal and the isolated
Python call, plus the unchanged four-argument render.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud.compose_helper import LOCKED_VERBS, VERB_BUDGET, helper_script
from tests.factories.compose_helper_sh import Runner, Tree, make_tree, runner_session

pytestmark = pytest.mark.unit

VERSION = "v0.99.1"
DIGEST = "ab" * 32


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


@pytest.fixture
def tree(runner: Runner, tmp_path: Path) -> Tree:
    return make_tree(tmp_path, runner)


def test_pin_metabase_is_a_budgeted_locked_verb() -> None:
    assert "pin-metabase" in VERB_BUDGET and "pin-metabase" in LOCKED_VERBS


def test_helper_text_runs_the_fixed_module_and_keeps_render_at_four_arguments() -> None:
    text = helper_script()
    assert (
        'locked env -i PATH="$PATH" LANG=C.UTF-8 "$S/venv/bin/python" -I'
        ' -m dango.platform.cloud.metabase_pin pin-custom "$1" "$2"'
    ) in text
    assert re.search(r"pin-metabase\)\s+root_only; argc \$# 2", text)
    assert re.search(r"render\)\s+argc \$# 4;", text)
    assert subprocess.run(["sh", "-n"], input=text, text=True, check=False).returncode == 0


def test_service_user_cannot_pin_metabase_digest(tree: Tree) -> None:
    refused = tree.call("pin-metabase", VERSION, DIGEST, sudo=True)
    assert refused.returncode == 64 and "root only" in refused.stderr
    assert tree.calls("python") == [] and tree.calls("docker") == []
    assert not (tree.compose / "metabase-jar.pin").exists()


def test_root_pin_metabase_runs_isolated_python_with_a_clean_environment(tree: Tree) -> None:
    result = tree.call("pin-metabase", VERSION, DIGEST, env={"PYTHONPATH": "/evil"})
    assert result.returncode == 0, result.stderr
    assert tree.calls("python") == [
        ["-I", "-m", "dango.platform.cloud.metabase_pin", "pin-custom", VERSION, DIGEST]
    ]
    assert tree.logs("python", "cwd")[0].read_text().strip() == "/"
    assert set(tree.env_of_call("python")) <= {"PATH", "LANG", "PWD"}
    assert tree.calls("docker") == []


PIN_REFUSALS: list[list[str]] = [
    [],
    [VERSION],
    [VERSION, DIGEST, "extra"],
    ["0.99.1", DIGEST],
    ["v0.9x", DIGEST],
    ["v" + "1" * 20, DIGEST],
    ["v0.99.1;id", DIGEST],
    [VERSION, "ab" * 31],
    [VERSION, DIGEST + "0"],
    [VERSION, "zz" * 32],
    [VERSION, "$(id)" + "a" * 59],
]


@pytest.mark.parametrize("args", PIN_REFUSALS, ids=[" ".join(a) or "empty" for a in PIN_REFUSALS])
def test_pin_metabase_grammar_refusals_never_reach_python(tree: Tree, args: list[str]) -> None:
    result = tree.call("pin-metabase", *args)
    assert result.returncode == 64, result.stderr
    assert tree.calls("python") == []
