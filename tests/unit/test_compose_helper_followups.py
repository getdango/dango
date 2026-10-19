"""tests/unit/test_compose_helper_followups.py

Compose helper follow-ups: a signal that arrives before the workload's pid is recorded
is still honoured, and the lock's lifetime caveat is documented next to the lock.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud.compose_helper import helper_script
from tests.factories.compose_helper_sh import Runner, make_tree, runner_session

pytestmark = pytest.mark.unit

SPAWN = 'timeout -k 10 "$t" "$@" </dev/null 9>&- & cp=$!'
LONG_DOCKER = """#!/bin/sh
echo $$ >"$(dirname "$0")/../dpid"
exec sleep 30
"""


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


def test_spawn_line_is_followed_by_a_pending_signal_check() -> None:
    text = helper_script()
    assert f'{SPAWN}\n  [ "$k" = 0 ] || kill -TERM "$cp" 2>/dev/null\n' in text


def test_signal_in_the_gap_before_the_pid_is_recorded_still_stops_the_workload(
    runner: Runner, tmp_path: Path
) -> None:
    tree = make_tree(tmp_path, runner)
    (tree.root / "shim" / "docker").write_text(LONG_DOCKER)
    (tree.root / "shim" / "docker").chmod(0o755)
    text = tree.script.read_text()
    assert text.count(SPAWN) == 1
    # A TERM delivered to the helper itself right before the workload is spawned.
    tree.script.write_text(text.replace(SPAWN, f"kill -TERM $$; {SPAWN}"))
    done = tree.call("stop", "metabase", sudo=True, timeout=20)
    assert done.returncode != 0
    pid = tree.root / "dpid"
    if pid.exists():
        alive = runner.run(
            ["sh", "-c", 'kill -0 "$0" 2>/dev/null && echo alive', pid.read_text().strip()]
        )
        assert alive.stdout.strip() == ""


def test_lock_comment_states_the_sigkill_caveat() -> None:
    lines = helper_script().splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("locked()"))
    comment = "\n".join(lines[max(0, at - 3) : at])
    assert "fd 9" in comment
    assert "SIGKILL" in comment
