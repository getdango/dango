"""tests/unit/test_server_compose.py

The server render procedure: pins, plugin jar staging, shared lock, import hygiene.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from dango.platform.cloud import server_compose as sc
from dango.platform.cloud.server_compose import Layout, RenderError, acquire_lock, main

pytestmark = pytest.mark.unit

MIB = 1024 * 1024
ARGS = ["3000", "8081", "v0.62.18", "my-project"]

_FAKE_PY = """#!{py}
import fcntl, json, os, sys, time
cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fakepy.json")))
args = sys.argv[1:]
reader = len(args) >= 6
if reader and cfg.get("lock"):
    fd = os.open(cfg["lock"], os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = "free"
    except BlockingIOError:
        state = "held"
    open(cfg["marker"], "a").write(state + "\\n")
mode = cfg.get("mode")
if reader and mode == "endless":
    open(cfg["pidfile"], "w").write(str(os.getpid()))
    chunk = b"x" * (1024 * 1024)
    while True:
        os.write(1, chunk)
if reader and mode == "sleep":
    open(cfg["pidfile"], "w").write(str(os.getpid()))
    time.sleep(120)
os.execv(sys.executable, [sys.executable] + args)
"""


_FAKE_GETENT = """#!/bin/sh
d=$(dirname "$0")
if [ -f "$d/hit.$1.$2" ]; then cat "$d/hit.$1.$2"; exit 0; fi
exit 2
"""


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.getent_dir = tmp / "getent-bin"
        self.getent_dir.mkdir()
        (self.getent_dir / "getent").write_text(_FAKE_GETENT)
        (self.getent_dir / "getent").chmod(0o755)
        self.srv = tmp / "srv"
        for rel in (
            "compose/build",
            "metabase",
            "project/data",
            "project/dbt/target",
            "project/metabase-plugins",
            "bin",
        ):
            (self.srv / rel).mkdir(parents=True)
        (tmp / "run").mkdir()
        (self.srv / "compose/project").write_text("dango-abcd1234\n")
        (self.srv / "compose/project-id").write_text("abcd1234-0000-4000-8000-000000000000\n")
        self.fakepy = self.srv / "bin/fakepy"
        self.fakepy.write_text(_FAKE_PY.format(py=sys.executable))
        self.fakepy.chmod(0o755)
        self.configure()
        self.layout = Layout(
            srv=str(self.srv),
            lock_dir=str(tmp / "run/dc"),
            owner_uid=os.geteuid(),
            lock_wait=0.3,
            reader_prefix=(str(self.fakepy),),
            jar_cap=2 * MIB,
            jar_timeout=2.0,
            total_timeout=8.0,
        )

    def host_has(self, database: str, key: str, line: str) -> None:
        """Make the fake ``getent`` report *line* for ``getent DATABASE KEY``."""
        (self.getent_dir / f"hit.{database}.{key}").write_text(line + "\n")

    def configure(self, **cfg: str) -> None:
        (self.srv / "bin/fakepy.json").write_text(json.dumps(cfg))

    @property
    def plugins_in(self) -> Path:
        return self.srv / "project/metabase-plugins"

    @property
    def plugins_out(self) -> Path:
        return self.srv / "metabase/plugins"

    def run(self, args: list[str] | None = None) -> int:
        return main(args or ARGS, self.layout)

    def leftovers(self) -> list[str]:
        found = [p.name for p in (self.srv / "metabase").iterdir() if p.name.startswith(".stage-")]
        found += [
            p.name for d in ("compose", "compose/build") for p in (self.srv / d).glob(".*.tmp")
        ]
        return found


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    made = Env(tmp_path)
    monkeypatch.setenv("PATH", f"{made.getent_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    return made


def test_render_writes_compose_dockerfile_and_entrypoint(env: Env) -> None:
    assert env.run() == 0
    compose = (env.srv / "compose/docker-compose.yml").read_text()
    assert "context: " + str(env.srv / "compose/build") in compose
    assert '"abcd1234-0000-4000-8000-000000000000"' in compose and '"my-project"' in compose
    docker = (env.srv / "compose/build/Dockerfile.metabase").read_text()
    assert docker.count("ENV MB_VERSION=v0.62.18\n") == 1
    entry = (env.srv / "compose/build/entrypoint.sh").read_bytes()
    assert entry.startswith(b"#!/bin/bash") and b"gosu 61000:61000" in entry
    for rel in (
        "compose/docker-compose.yml",
        "compose/build/Dockerfile.metabase",
        "compose/build/entrypoint.sh",
    ):
        assert (env.srv / rel).stat().st_mode & 0o777 == 0o644
    assert env.leftovers() == []


@pytest.mark.parametrize(
    "args",
    [
        ARGS[:3],
        [*ARGS, "abcd1234-0000-4000-8000-000000000000"],
        ["80", "8081", "v0.59.1", "n"],
        ["3000", "3000", "v0.59.1", "n"],
        ["3000", "8081", "0.59.1", "n"],
        ["3000", "8081", "v0.59.1;id", "n"],
        ["03000", "8081", "v0.59.1", "n"],
    ],
)
def test_arguments_are_checked_again_and_no_id_is_accepted(env: Env, args: list[str]) -> None:
    assert env.run(args) == 64
    assert not (env.srv / "compose/docker-compose.yml").exists()


@pytest.mark.parametrize("name", ['a"\n    privileged: true', 'a"b', "a\\b", "a\nb"])
def test_unquotable_name_is_refused_and_nothing_is_written(env: Env, name: str) -> None:
    (env.srv / "compose/docker-compose.yml").write_text("old\n")
    assert env.run([*ARGS[:3], name]) == 66
    assert (env.srv / "compose/docker-compose.yml").read_text() == "old\n"
    assert env.leftovers() == []


@pytest.mark.parametrize("breakage", ["missing_id", "mismatch", "bad_name", "symlink"])
def test_render_needs_consistent_root_pins(env: Env, breakage: str) -> None:
    pin = env.srv / "compose/project"
    if breakage == "missing_id":
        (env.srv / "compose/project-id").unlink()
    elif breakage == "mismatch":
        (env.srv / "compose/project-id").write_text("deadbeef-0000\n")
    elif breakage == "bad_name":
        pin.write_text("dango-ABCD1234\n")
    else:
        pin.unlink()
        pin.symlink_to(env.srv / "compose/project-id")
    assert env.run() == 64
    assert not (env.srv / "compose/docker-compose.yml").exists()


def test_jars_are_staged_replaced_and_stale_ones_removed(env: Env) -> None:
    (env.plugins_in / "duckdb.jar").write_bytes(b"jar-one")
    (env.plugins_in / "notes.txt").write_text("ignored")
    env.plugins_out.mkdir(mode=0o755)
    (env.plugins_out / "stale.jar").write_bytes(b"old")
    (env.plugins_out / "keep.txt").write_text("kept")
    assert env.run() == 0
    assert (env.plugins_out / "duckdb.jar").read_bytes() == b"jar-one"
    assert (env.plugins_out / "duckdb.jar").stat().st_mode & 0o777 == 0o644
    assert not (env.plugins_out / "stale.jar").exists() and (env.plugins_out / "keep.txt").exists()
    assert not (env.plugins_out / "notes.txt").exists() and env.leftovers() == []


@pytest.mark.parametrize("kind", ["bad_name", "too_many", "fifo", "symlink", "oversize"])
def test_bad_jars_fail_fast_naming_the_cause_and_change_nothing(env: Env, kind: str) -> None:
    env.plugins_out.mkdir(mode=0o755)
    (env.plugins_out / "existing.jar").write_bytes(b"live")
    if kind == "bad_name":
        (env.plugins_in / "bad name.jar").write_bytes(b"x")
    elif kind == "too_many":
        for i in range(11):
            (env.plugins_in / f"j{i}.jar").write_bytes(b"x")
    elif kind == "fifo":
        os.mkfifo(env.plugins_in / "pipe.jar")
    elif kind == "symlink":
        (env.plugins_in / "target").write_bytes(b"secret")
        (env.plugins_in / "link.jar").symlink_to(env.plugins_in / "target")
    else:
        with open(env.plugins_in / "big.jar", "wb") as handle:
            handle.truncate(3 * MIB)
    started = time.monotonic()
    assert env.run() == 65
    assert time.monotonic() - started < 6
    assert (env.plugins_out / "existing.jar").read_bytes() == b"live"
    assert not (env.srv / "compose/docker-compose.yml").exists() and env.leftovers() == []


def pid_gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.mark.parametrize("mode", ["endless", "sleep"])
def test_a_runaway_reader_is_killed_at_the_cap_or_the_timeout(env: Env, mode: str) -> None:
    (env.plugins_in / "a.jar").write_bytes(b"x")
    env.configure(mode=mode, pidfile=str(env.tmp / "pid"))
    started = time.monotonic()
    assert env.run() == 65
    assert time.monotonic() - started < 6
    assert pid_gone(int((env.tmp / "pid").read_text()))
    assert (
        not (env.plugins_out.exists() and list(env.plugins_out.glob("*.jar")))
        and env.leftovers() == []
    )


def test_staging_happens_without_the_lock(env: Env) -> None:
    (env.plugins_in / "a.jar").write_bytes(b"x")
    lock = env.tmp / "run/dc"
    lock.mkdir(mode=0o700)
    (lock / "lock").touch(mode=0o600)
    env.configure(lock=str(lock / "lock"), marker=str(env.tmp / "marker"))
    assert env.run() == 0
    assert (env.tmp / "marker").read_text().split() == ["free"]


def test_busy_lock_exits_75_before_any_final_write(env: Env) -> None:
    (env.plugins_in / "a.jar").write_bytes(b"x")
    (env.srv / "compose/docker-compose.yml").write_text("old\n")
    fd = acquire_lock(env.layout.lock_dir, os.geteuid(), 1.0)
    try:
        assert env.run() == 75
    finally:
        os.close(fd)
    assert (env.srv / "compose/docker-compose.yml").read_text() == "old\n"
    assert not (env.plugins_out / "a.jar").exists() and env.leftovers() == []


class TestLockRules:
    """The same directory/file rules as the helper's ``lockdir`` (see test_compose_helper_lock.py)."""

    def test_first_call_creates_dir_0700_and_file_0600(self, tmp_path: Path) -> None:
        lock_dir = tmp_path / "dc"
        os.close(acquire_lock(str(lock_dir), os.geteuid(), 1.0))
        assert (
            lock_dir.stat().st_mode & 0o777 == 0o700
            and (lock_dir / "lock").stat().st_mode & 0o777 == 0o600
        )

    @pytest.mark.parametrize(
        "breakage", ["dir_0755", "dir_symlink", "file_0644", "file_symlink", "wrong_owner"]
    )
    def test_unsafe_lock_paths_are_refused(self, tmp_path: Path, breakage: str) -> None:
        lock_dir = tmp_path / "dc"
        lock_dir.mkdir(mode=0o700)
        lock_dir.chmod(0o700)
        (lock_dir / "lock").touch(mode=0o600)
        (lock_dir / "lock").chmod(0o600)
        owner = os.geteuid()
        if breakage == "dir_0755":
            lock_dir.chmod(0o755)
        elif breakage == "dir_symlink":
            lock_dir = tmp_path / "link"
            lock_dir.symlink_to(tmp_path / "dc")
        elif breakage == "file_0644":
            (lock_dir / "lock").chmod(0o644)
        elif breakage == "file_symlink":
            (lock_dir / "lock").unlink()
            (lock_dir / "lock").symlink_to(tmp_path / "x")
            (tmp_path / "x").touch(mode=0o600)
        else:
            owner = os.geteuid() + 1
        with pytest.raises(RenderError) as err:
            acquire_lock(str(lock_dir), owner, 0.2)
        assert err.value.code == 64 and "unsafe" in str(err.value)

    def test_busy_lock_raises_75_after_the_wait(self, tmp_path: Path) -> None:
        first = acquire_lock(str(tmp_path / "dc"), os.geteuid(), 1.0)
        try:
            started = time.monotonic()
            with pytest.raises(RenderError) as err:
                acquire_lock(str(tmp_path / "dc"), os.geteuid(), 0.3)
        finally:
            os.close(first)
        assert err.value.code == 75 and 0.25 <= time.monotonic() - started < 3


def test_importing_the_server_code_reads_no_relative_path_home_or_dotenv(tmp_path: Path) -> None:
    """Run as the helper does (``python -I`` from another directory): only absolute, non-HOME opens."""
    cwd = tmp_path / "cwd"
    home = tmp_path / "home"
    for d in (cwd, home):
        d.mkdir()
        (d / ".env").write_text("PYTHONPATH=/evil\nDOCKER_HOST=tcp://203.0.113.1:2375\n")
    (cwd / "jinja2.py").write_text("raise SystemExit('planted module imported')\n")
    probe = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(Path(sc.__file__).parents[3])!r})
        opened = []
        sys.addaudithook(lambda ev, a: opened.append(a[0]) if ev == "open" else None)
        import dango.platform.cloud.server_compose as m
        bad = [p for p in opened if isinstance(p, str) and not p.startswith(("/", "__editable__"))]
        bad += [p for p in opened if isinstance(p, str) and p.startswith(({str(home)!r}, {str(cwd)!r}))]
        print(m.__name__, bad)
        """
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": "/evil"}
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("[]"), result.stdout


def test_the_module_never_consults_the_environment_or_home() -> None:
    source = Path(sc.__file__).read_text()
    for needle in ("os.environ", "getenv", "expanduser", "Path.home", "getcwd", "dotenv"):
        assert needle not in source


def test_server_render_pins_container_ids_in_every_rendered_file(env: Env) -> None:
    assert env.run() == 0
    compose = (env.srv / "compose/docker-compose.yml").read_text()
    assert 'user: "61001:61001"' in compose
    docker = (env.srv / "compose/build/Dockerfile.metabase").read_text()
    assert "groupadd -r -g 61000 metabase" in docker and "useradd -r -u 61000 -g metabase" in docker
    entry = (env.srv / "compose/build/entrypoint.sh").read_text()
    assert "chown -R 61000:61000 /app/plugins" in entry and "gosu 61000:61000" in entry


def test_the_packaged_files_used_by_local_projects_keep_the_default_ids() -> None:
    root = Path(sc.__file__).resolve().parents[2] / "templates"
    for name in ("Dockerfile.metabase", "entrypoint.sh"):
        assert "6100" not in (root / name).read_text()


CLASHES = [
    ("passwd", "61000", "x:x:61000:61000::/:/bin/false"),
    ("passwd", "61001", "x:x:61001:100::/:/bin/false"),
    ("group", "61000", "x:x:61000:"),
    ("group", "61001", "x:x:61001:"),
    ("group", "docker", "docker:x:61001:"),
]


@pytest.mark.parametrize(("database", "key", "line"), CLASHES)
def test_render_refuses_when_the_host_uses_a_container_id(
    env: Env, database: str, key: str, line: str
) -> None:
    env.host_has(database, key, line)
    assert env.run() == 67
    assert not (env.srv / "compose/docker-compose.yml").exists()
    assert env.leftovers() == []


def test_render_refuses_when_the_host_ids_cannot_be_checked(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(env.srv / "bin-missing"))
    assert env.run() == 67
    assert not (env.srv / "compose/docker-compose.yml").exists()


def test_other_docker_group_ids_are_fine(env: Env) -> None:
    env.host_has("group", "docker", "docker:x:999:")
    assert env.run() == 0


@pytest.mark.parametrize("name", ["a$b", "${HOME}", "$(id)", "$$"])
def test_dollar_in_the_project_name_is_refused(env: Env, name: str) -> None:
    assert env.run([*ARGS[:3], name]) == 66
    assert not (env.srv / "compose/docker-compose.yml").exists()


def test_an_os_error_while_installing_exits_65_and_cleans_up(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(src: str, dst: str) -> None:
        raise PermissionError(13, "denied", dst)

    monkeypatch.setattr(sc.os, "replace", boom)
    assert env.run() == 65
    assert env.leftovers() == []


def test_jars_are_replaced_before_the_compose_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    (env.plugins_in / "duckdb.jar").write_bytes(b"jar")
    order: list[str] = []
    real = os.replace

    def spy(src: str, dst: str) -> None:
        order.append(os.path.basename(dst))
        real(src, dst)

    monkeypatch.setattr(sc.os, "replace", spy)
    assert env.run() == 0
    assert order.index("duckdb.jar") < order.index("docker-compose.yml")
