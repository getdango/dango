"""tests/unit/test_compose_helper_sh.py

The compose helper's sh text run under /bin/sh against a temp tree with a fake docker.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from dango.platform.cloud.compose_helper import VERB_BUDGET, helper_script
from tests.factories.compose_helper_sh import (
    PROD_PATH_LINE,
    PROD_ROOT_LINE,
    PROD_WAIT_LINE,
    Runner,
    Tree,
    make_tree,
    patched_script,
    runner_session,
)

pytestmark = pytest.mark.unit

PROJECT = "dango-abcd1234"
BASE = ["compose", "--project-directory", "{D}", "-p", PROJECT, "-f", "{D}/docker-compose.yml"]


@pytest.fixture(scope="module")
def runner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Runner]:
    with runner_session(tmp_path_factory.getbasetemp()) as r:
        yield r


@pytest.fixture
def tree(runner: Runner, tmp_path: Path) -> Tree:
    return make_tree(tmp_path, runner)


def expected(tree: Tree, *tokens: str) -> list[str]:
    return [t.replace("{D}", str(tree.compose)) for t in BASE] + list(tokens)


def test_patched_script_differs_only_in_path_root_and_wait_lines(tmp_path: Path) -> None:
    prod = helper_script().splitlines()
    patched = patched_script(tmp_path, 1, 2).splitlines()
    assert len(prod) == len(patched)
    changed = {a for a, b in zip(prod, patched, strict=True) if a != b}
    assert changed == {PROD_PATH_LINE, PROD_ROOT_LINE, PROD_WAIT_LINE}


GRAMMAR_REFUSALS: list[list[str]] = [
    [],
    ["frobnicate"],
    ["up", "x"],
    ["down", "-v"],
    ["run", "--rm", "metabase", "sh"],
    ["exec", "metabase", "sh"],
    ["up", "-f", "x.yml"],
    ["--project-directory", "/tmp", "up"],
    ["stop", "x"],
    ["start", "metabase", "dbt-docs"],
    ["restart"],
    ["restart", "x"],
    ["ps", "x"],
    ["image-exists", "x"],
    ["h2-size", "x"],
    ["self-test", "x"],
    ["logs"],
    ["logs", "x"],
    ["logs", "metabase", "--since", "$(id)"],
    ["logs", "metabase", "--since", "2026-01-01T00:00:00Z;id"],
    ["logs", "metabase", "--since", "x-01-01"],
    ["logs", "metabase", "--since"],
    ["logs", "metabase", "--tail", "0"],
    ["logs", "metabase", "--tail", "123456"],
    ["logs", "metabase", "--bogus"],
    ["reset-password"],
    ["reset-password", "a b"],
    ["reset-password", "-x@y.z"],
    ["reset-password", "a@b@c.d"],
    ["reset-password", "nodomain"],
    ["reset-password", "a@b.c", "extra"],
    ["render", "80", "8081", "v0.59.1", "n"],
    ["render", "70000", "8081", "v0.59.1", "n"],
    ["render", "0123", "8081", "v0.59.1", "n"],
    ["render", "3000", "3000", "v0.59.1", "n"],
    ["render", "3000", "8081", "v0.59.1", "n", "abcd1234-id"],
    ["render", "3000", "8081", "0.59.1", "n"],
    ["render", "3000", "8081", "v0.5x", "n"],
    ["render", "3000", "8081", "v" + "1" * 20, "n"],
    ["pin-project"],
    ["pin-project", "zzzzzzzz"],
    ["pin-project", "deadbeef", "--bogus"],
    ["down-volumes", "dbt-docs"],
    ["down-volumes"],
    ["up-build", "x"],
    ["destroy", "x"],
]


@pytest.mark.parametrize(
    "args", GRAMMAR_REFUSALS, ids=[" ".join(a) or "empty" for a in GRAMMAR_REFUSALS]
)
def test_grammar_refusals_exit_64_and_never_reach_docker(tree: Tree, args: list[str]) -> None:
    result = tree.call(*args)
    assert result.returncode == 64, result.stderr
    assert result.stderr.startswith("dango-compose:")
    assert tree.calls("docker") == [] and tree.calls("python") == []


@pytest.mark.parametrize(
    "args",
    [
        ["pin-project", "deadbeef"],
        ["up-build"],
        ["pull"],
        ["build"],
        ["down-volumes", "metabase"],
        ["down-rmi"],
        ["destroy"],
        ["volume-path"],
    ],
)
def test_root_only_verbs_are_refused_through_sudo(tree: Tree, args: list[str]) -> None:
    refused = tree.call(*args, sudo=True)
    assert refused.returncode == 64 and "root only" in refused.stderr
    assert tree.calls("docker") == []
    assert tree.call(*args).returncode == 0 or args[0] == "pin-project"  # root is allowed


@pytest.mark.parametrize(
    ("args", "tokens"),
    [
        (["up"], ["up", "-d"]),
        (["down"], ["down"]),
        (["stop"], ["stop"]),
        (["stop", "metabase"], ["stop", "metabase"]),
        (["start", "dbt-docs"], ["start", "dbt-docs"]),
        (["restart", "metabase"], ["restart", "metabase"]),
        (["ps"], ["ps", "--format", "json"]),
        (["image-exists"], ["images", "-q", "metabase"]),
        (
            ["h2-size"],
            ["exec", "-T", "metabase", "stat", "-c", "%s", "/metabase-data/metabase.db.mv.db"],
        ),
        (["logs", "metabase"], ["logs", "--no-color", "metabase"]),
        (
            ["logs", "metabase", "--tail", "50", "--since", "2026-01-01T00:00:00Z"],
            ["logs", "--no-color", "--since", "2026-01-01T00:00:00Z", "--tail", "50", "metabase"],
        ),
        (
            ["reset-password", "a.b+c@example.com"],
            ["run", "--rm", "--no-deps", "-T", "metabase", "reset-password", "a.b+c@example.com"],
        ),
    ],
)
def test_allowed_verbs_pass_only_fixed_tokens_with_stdin_closed(
    tree: Tree, args: list[str], tokens: list[str]
) -> None:
    result = tree.call(*args, sudo=True)
    assert result.returncode == 0, result.stderr
    assert tree.calls("docker") == [expected(tree, *tokens)]
    assert (
        tree.logs("docker", "stdin")[0].read_text() == ""
    )  # the caller's input never reaches Compose


@pytest.mark.parametrize(
    ("args", "tokens"),
    [
        (["up-build"], ["up", "-d", "--build", "metabase"]),
        (["pull"], ["pull"]),
        (["build"], ["build"]),
        (["down-volumes", "metabase"], ["down", "metabase", "--volumes"]),
        (["down-rmi"], ["down", "--rmi", "local"]),
        (["destroy"], ["down", "--volumes", "--rmi", "local"]),
    ],
)
def test_root_verbs_pass_only_fixed_tokens(tree: Tree, args: list[str], tokens: list[str]) -> None:
    assert tree.call(*args).returncode == 0
    assert tree.calls("docker") == [expected(tree, *tokens)]


def test_volume_path_inspects_the_pinned_volume(tree: Tree) -> None:
    assert tree.call("volume-path").returncode == 0
    assert tree.calls("docker") == [
        ["volume", "inspect", f"{PROJECT}_metabase-data", "--format", "{{.Mountpoint}}"]
    ]


def test_environment_is_cleared_before_docker(tree: Tree) -> None:
    evil = {
        "DOCKER_HOST": "tcp://203.0.113.1:2375",
        "COMPOSE_FILE": "/tmp/evil.yml",
        "COMPOSE_PROJECT_NAME": "evil",
        "DOCKER_CONFIG": "/tmp/evil",
        "HOME": "/home/evil",
    }
    assert tree.call("ps", sudo=True, env=evil).returncode == 0
    seen = tree.env_of_call()
    assert not set(seen) & set(evil) - {"HOME"} and seen["HOME"] == "/root"
    assert seen["LANG"] == "C.UTF-8" and seen["PATH"].startswith(str(tree.root / "shim"))
    assert set(seen) <= {"PATH", "HOME", "LANG", "PWD"}


@pytest.mark.parametrize("verb", [["ps"], ["up"], ["restart", "metabase"]])
@pytest.mark.parametrize(
    "breakage",
    [
        "compose_file_group_writable",
        "compose_file_symlink",
        "srv_group_writable",
        "build_symlink",
        "metabase_group_writable",
        "project_group_writable",
    ],
)
def test_unowned_inputs_are_refused_by_every_compose_verb(
    tree: Tree, verb: list[str], breakage: str
) -> None:
    c = tree.compose
    if breakage == "compose_file_group_writable":
        (c / "docker-compose.yml").chmod(0o664)
    elif breakage == "compose_file_symlink":
        (c / "real.yml").write_text("services: {}\n")
        (c / "docker-compose.yml").unlink()
        (c / "docker-compose.yml").symlink_to(c / "real.yml")
    elif breakage == "srv_group_writable":
        tree.srv.chmod(0o775)
    elif breakage == "build_symlink":
        (c / "build").rmdir()
        (c / "build").symlink_to(tree.srv)
    elif breakage == "metabase_group_writable":
        (tree.srv / "metabase").chmod(0o770)
    else:
        (c / "project").chmod(0o664)
    result = tree.call(*verb, sudo=True)
    assert result.returncode == 64 and "not root-owned" in result.stderr
    assert tree.calls("docker") == []


def test_project_name_must_be_a_pinned_dango_name(tree: Tree) -> None:
    (tree.compose / "project").write_text("evil; id\n")
    result = tree.call("ps", sudo=True)
    assert result.returncode == 64 and "bad project" in result.stderr


def test_pin_project_only_by_root_and_never_silently_replaced(
    runner: Runner, tmp_path: Path
) -> None:
    tree = make_tree(tmp_path, runner, pinned=False)
    ident = "deadbeef-1111-4222-8333-444444444444"
    assert tree.call("pin-project", ident).returncode == 0
    assert (tree.compose / "project").read_text() == "dango-deadbeef\n"
    assert (tree.compose / "project-id").read_text() == ident + "\n"
    other = tree.call("pin-project", "cafebabe-0000")
    assert other.returncode == 64 and "pinned" in other.stderr
    assert (tree.compose / "project").read_text() == "dango-deadbeef\n"
    assert tree.call("pin-project", "cafebabe-0000", "--replace").returncode == 0
    assert (tree.compose / "project").read_text() == "dango-cafebabe\n"


def test_render_runs_isolated_python_from_root_with_a_clean_environment(tree: Tree) -> None:
    result = tree.call(
        "render", "3000", "8081", "v0.59.1", "my-project", sudo=True, env={"PYTHONPATH": "/evil"}
    )
    assert result.returncode == 0, result.stderr
    assert tree.calls("python") == [
        ["-I", "-m", "dango.platform.cloud.server_compose", "3000", "8081", "v0.59.1", "my-project"]
    ]
    assert tree.logs("python", "cwd")[0].read_text().strip() == "/"
    assert set(tree.env_of_call("python")) <= {"PATH", "LANG", "PWD"}
    assert tree.calls("docker") == []


def test_self_test_passes_without_a_project_and_runs_python_from_root(
    runner: Runner, tmp_path: Path
) -> None:
    tree = make_tree(tmp_path, runner, pinned=False)
    result = tree.call("self-test", sudo=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    assert tree.calls("python") == [["-I", "-c", "import dango.platform.cloud.server_compose"]]
    assert tree.logs("python", "cwd")[0].read_text().strip() == "/"
    assert tree.calls("docker") == [["compose", "version", "--short"]]


@pytest.mark.parametrize(
    ("tool", "message"), [("docker", "compose v2 missing"), ("python", "server code")]
)
def test_self_test_names_the_missing_piece(
    runner: Runner, tmp_path: Path, tool: str, message: str
) -> None:
    tree = make_tree(tmp_path, runner, pinned=False)
    tree.set_tool(tool, rc=1)
    result = tree.call("self-test", sudo=True)
    assert result.returncode == 64 and message in result.stderr and "ok" not in result.stdout


def test_self_test_names_a_missing_flock(runner: Runner, tmp_path: Path) -> None:
    tree = make_tree(tmp_path, runner, pinned=False)
    farm = tmp_path / "farm"
    farm.mkdir()
    for tool in (
        "timeout",
        "env",
        "cat",
        "cut",
        "mv",
        "mkdir",
        "sleep",
        "dirname",
        "basename",
        "pwd",
    ):
        found = runner.run(["sh", "-c", f"command -v {tool}"])
        if found.returncode == 0 and found.stdout.startswith("/"):
            (farm / tool).symlink_to(found.stdout.strip())
    text = tree.script.read_text().replace(
        f"PATH={tree.root / 'shim'}:", f"PATH={tree.root / 'shim'}:{farm}:", 1
    )
    text = text.replace(
        ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin; export PATH",
        "; export PATH",
    )
    tree.script.write_text(text)
    result = tree.call("self-test", sudo=True)
    assert result.returncode == 64 and "flock missing" in result.stderr


def test_ps_does_not_create_the_lock_directory(tree: Tree) -> None:
    assert tree.call("ps", sudo=True).returncode == 0
    assert not tree.lock_dir.exists()


def test_ownership_checks_with_real_root_and_real_find(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """In a container as uid 0, with real find/id (no shims): the root-owned checks really bite."""
    if not os.environ.get("DANGO_RUN_DOCKER_TESTS"):  # repo gate for Docker tests
        pytest.skip("set DANGO_RUN_DOCKER_TESTS=1 to run Docker tests")
    with runner_session(tmp_path_factory.getbasetemp(), user="0") as r:
        sh = "/tmp/t"
        root = Path(sh)
        build = (
            "set -e; mkdir -p /tmp/t/srv/compose/build /tmp/t/srv/metabase /tmp/t/srv/venv /tmp/t/shim /tmp/t/run;"
            " chmod 0755 /tmp/t/srv /tmp/t/srv/compose /tmp/t/srv/compose/build /tmp/t/srv/metabase;"
            " echo dango-abcd1234 > /tmp/t/srv/compose/project; echo 'services: {}' > /tmp/t/srv/compose/docker-compose.yml;"
            " printf '#!/bin/sh\\nexit 0\\n' > /tmp/t/shim/docker; chmod 0755 /tmp/t/shim/docker"
        )
        assert r.run(["sh", "-c", build]).returncode == 0
        text = patched_script(root, 1, 2)
        assert r.run(["sh", "-c", "cat > /tmp/t/h.sh"], stdin=text).returncode == 0

        def helper() -> int:
            return r.run(["sh", "/tmp/t/h.sh", "ps"], env={"SUDO_UID": "1000"}).returncode

        assert helper() == 0
        r.run(["chmod", "0664", "/tmp/t/srv/compose/docker-compose.yml"])
        assert helper() == 64
        r.run(["chmod", "0644", "/tmp/t/srv/compose/docker-compose.yml"])
        assert helper() == 0
        r.run(["chown", "65534", "/tmp/t/srv/compose/docker-compose.yml"])
        assert helper() == 64
        r.run(["chown", "0", "/tmp/t/srv/compose/docker-compose.yml"])
        assert helper() == 0


def test_budgets_cover_every_verb_in_the_script() -> None:
    text = helper_script()
    for verb in VERB_BUDGET:
        assert f"  {verb}) echo {VERB_BUDGET[verb]};;" in text
        assert verb in text.split('case "$verb" in', 1)[1]
