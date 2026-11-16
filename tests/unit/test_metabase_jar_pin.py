"""tests/unit/test_metabase_jar_pin.py

Metabase jar integrity: the server render binds the jar download to a SHA-256, the default
from the packaged recipe and a custom version only from a root-owned administrator pin.
"""

from __future__ import annotations

import dataclasses
import importlib
import os
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from dango.platform.cloud import server_compose as sc
from dango.platform.cloud.server_compose import Layout

pytestmark = pytest.mark.unit

DEFAULT_VERSION = "v0.62.18"
DEFAULT_DIGEST = "4cc9c801ac616e4b5093c4906cb59f6e8f819f620c65d94ca4469ed87034a2e5"
CUSTOM = "v0.99.1"
DIGEST_A = "ab" * 32
DIGEST_B = "cd" * 32
TEMPLATE = Path(sc.__file__).parents[2] / "templates"
ARGS = ["3000", "8081"]


def pin_module() -> ModuleType:
    return importlib.import_module("dango.platform.cloud.metabase_pin")


class Server:
    """A temp server tree with a rendered-before state that failures must leave untouched."""

    def __init__(self, tmp: Path) -> None:
        self.srv = tmp / "srv"
        for rel in ("compose/build", "metabase", "project/data", "project/dbt/target"):
            (self.srv / rel).mkdir(parents=True)
        for rel in ("compose", "compose/build", "metabase"):
            (self.srv / rel).chmod(0o755)
        (tmp / "run").mkdir()
        (self.srv / "compose/project").write_text("dango-abcd1234\n")
        (self.srv / "compose/project-id").write_text("abcd1234-0000-4000-8000-000000000000\n")
        self.layout = Layout(
            srv=str(self.srv),
            lock_dir=str(tmp / "run/dc"),
            owner_uid=os.geteuid(),
            lock_wait=0.3,
            reader_prefix=(sys.executable,),
        )
        self.installed = {
            self.srv / "compose/docker-compose.yml": b"installed compose\n",
            self.srv / "compose/build/Dockerfile.metabase": b"installed dockerfile\n",
            self.srv / "compose/build/entrypoint.sh": b"installed entrypoint\n",
        }
        for path, data in self.installed.items():
            path.write_bytes(data)

    @property
    def pin_path(self) -> Path:
        return self.srv / "compose/metabase-jar.pin"

    @property
    def dockerfile(self) -> str:
        return (self.srv / "compose/build/Dockerfile.metabase").read_text()

    def write_pin(self, text: str, mode: int = 0o600) -> None:
        self.pin_path.write_text(text)
        self.pin_path.chmod(mode)

    def render(self, version: str, layout: Layout | None = None) -> int:
        return sc.main([*ARGS, version, "my-project"], layout or self.layout)

    def assert_untouched(self) -> None:
        for path, data in self.installed.items():
            assert path.read_bytes() == data, path
        leftovers = [p.name for p in (self.srv / "compose").rglob(".*.tmp")]
        leftovers += [p.name for p in (self.srv / "metabase").iterdir() if p.name != "plugins"]
        assert leftovers == []


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Server:
    monkeypatch.setattr(sc, "check_container_ids", lambda: None)
    return Server(tmp_path)


def remote_adds(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"ADD\s", ln) and "https://" in ln]


def assert_bound_to(text: str, digest: str) -> None:
    adds = remote_adds(text)
    assert len(adds) == 1
    assert adds[0].startswith(f"ADD --checksum=sha256:{digest} https://downloads.metabase.com/")
    assert text.count("--checksum=") == 1
    assert text.count(digest) == 2, "digest only on the ADD and the sha256sum check"
    assert sha_check_lines(text, digest) == 1


def sha_check_lines(text: str, digest: str) -> int:
    check = f'RUN echo "{digest}  /app/metabase.jar" | sha256sum -c -'
    return text.splitlines().count(check)


def test_default_metabase_jar_uses_pinned_checksum(server: Server) -> None:
    assert server.render(DEFAULT_VERSION) == 0
    assert_bound_to(server.dockerfile, DEFAULT_DIGEST)
    assert f"ENV MB_VERSION={DEFAULT_VERSION}\n" in server.dockerfile
    assert server.dockerfile.startswith("# syntax=docker/dockerfile:1.6\n")


def test_default_version_ignores_a_custom_pin_file(server: Server) -> None:
    server.write_pin(f"{DEFAULT_VERSION} {DIGEST_A}\n")
    assert server.render(DEFAULT_VERSION) == 0
    assert_bound_to(server.dockerfile, DEFAULT_DIGEST)


def test_packaged_recipe_carries_the_default_digest_and_syntax_directive() -> None:
    text = (TEMPLATE / "Dockerfile.metabase").read_text()
    assert text.splitlines()[0] == "# syntax=docker/dockerfile:1.6"
    assert_bound_to(text, DEFAULT_DIGEST)
    assert text.count(f"ENV MB_VERSION={DEFAULT_VERSION}\n") == 1


def test_rendered_recipe_binds_every_remote_add_to_the_pinned_digest(server: Server) -> None:
    """A wrong or missing digest must make the build fail, so no download may go unchecked."""
    server.write_pin(f"{CUSTOM} {DIGEST_A}\n")
    assert server.render(CUSTOM) == 0
    text = server.dockerfile
    assert remote_adds(text), "the recipe downloads the jar"
    for line in remote_adds(text):
        assert re.fullmatch(rf"ADD --checksum=sha256:{DIGEST_A} \S+ \S+", line), line
    assert DEFAULT_DIGEST not in text
    assert text.count(DIGEST_A) == 2
    assert sha_check_lines(text, DIGEST_A) == 1


def test_custom_metabase_version_uses_root_pin(server: Server) -> None:
    server.write_pin(f"{CUSTOM} {DIGEST_A}\n")
    assert server.render(CUSTOM) == 0
    assert f"ENV MB_VERSION={CUSTOM}\n" in server.dockerfile
    assert_bound_to(server.dockerfile, DIGEST_A)


@pytest.mark.parametrize("pin", [None, f"v0.98.0 {DIGEST_A}\n"], ids=["absent", "other-version"])
def test_custom_version_without_matching_pin_preserves_build_files(
    server: Server, pin: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    if pin is not None:
        server.write_pin(pin)
    assert server.render(CUSTOM) == 68
    err = capsys.readouterr().err
    assert "pin-metabase" in err and CUSTOM in err
    server.assert_untouched()


def _symlink(server: Server) -> None:
    real = server.srv / "elsewhere.pin"
    real.write_text(f"{CUSTOM} {DIGEST_A}\n")
    real.chmod(0o600)
    server.pin_path.symlink_to(real)


def _directory(server: Server) -> None:
    server.pin_path.mkdir()


def _fifo(server: Server) -> None:
    os.mkfifo(server.pin_path, 0o600)


UNSAFE_TEXT = {
    "short-digest": f"{CUSTOM} {DIGEST_A[:-2]}\n",
    "non-hex-digest": f"{CUSTOM} {'zz' * 32}\n",
    "upper-digest": f"{CUSTOM} {DIGEST_A.upper()}\n",
    "truncated": f"{CUSTOM} {DIGEST_A}",
    "extra-field": f"{CUSTOM} {DIGEST_A} extra\n",
    "two-lines": f"{CUSTOM} {DIGEST_A}\n{CUSTOM} {DIGEST_B}\n",
    "empty": "",
    "oversized": "x" * 100_000,
}


@pytest.mark.parametrize("name", sorted(UNSAFE_TEXT))
def test_unsafe_metabase_pin_is_rejected(server: Server, name: str) -> None:
    server.write_pin(UNSAFE_TEXT[name])
    assert server.render(CUSTOM) == 68
    server.assert_untouched()


@pytest.mark.parametrize("mode", [0o666, 0o664, 0o662, 0o606])
def test_unsafe_metabase_pin_mode_is_rejected(server: Server, mode: int) -> None:
    server.write_pin(f"{CUSTOM} {DIGEST_A}\n", mode)
    assert server.render(CUSTOM) == 68
    server.assert_untouched()


@pytest.mark.parametrize("make", [_symlink, _directory, _fifo], ids=["symlink", "dir", "fifo"])
def test_unsafe_metabase_pin_file_type_is_rejected(
    server: Server, make: Callable[[Server], None]
) -> None:
    make(server)
    assert server.render(CUSTOM) == 68
    server.assert_untouched()


def test_pin_owned_by_another_user_is_rejected(server: Server) -> None:
    server.write_pin(f"{CUSTOM} {DIGEST_A}\n")
    other = dataclasses.replace(server.layout, owner_uid=os.geteuid() + 1)
    assert server.render(CUSTOM, other) == 68
    server.assert_untouched()


def test_pin_in_a_group_writable_directory_is_rejected(server: Server) -> None:
    server.write_pin(f"{CUSTOM} {DIGEST_A}\n")
    (server.srv / "compose").chmod(0o775)
    assert server.render(CUSTOM) == 68
    (server.srv / "compose").chmod(0o755)
    server.assert_untouched()


def test_admin_pin_update_changes_rendered_checksum(server: Server) -> None:
    mp = pin_module()
    mp.pin_custom(CUSTOM, DIGEST_A, server.layout)
    assert server.render(CUSTOM) == 0
    assert_bound_to(server.dockerfile, DIGEST_A)
    mp.pin_custom("v0.99.2", DIGEST_B.upper(), server.layout)
    assert server.render(CUSTOM) == 68  # the old version no longer has a pin
    assert server.render("v0.99.2") == 0
    assert "ENV MB_VERSION=v0.99.2\n" in server.dockerfile
    assert_bound_to(server.dockerfile, DIGEST_B)


def test_pin_custom_writes_one_private_record_without_leftovers(server: Server) -> None:
    mp = pin_module()
    mp.pin_custom(CUSTOM, DIGEST_A, server.layout)
    assert server.pin_path.read_text() == f"{CUSTOM} {DIGEST_A}\n"
    assert server.pin_path.stat().st_mode & 0o777 == 0o600
    assert server.pin_path.stat().st_uid == os.geteuid()
    assert [p.name for p in (server.srv / "compose").glob(".*")] == []
    assert mp.digest_for_version(CUSTOM, server.layout) == DIGEST_A
    assert mp.digest_for_version(DEFAULT_VERSION, server.layout) == DEFAULT_DIGEST


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        (CUSTOM, "ab" * 31),
        (CUSTOM, "zz" * 32),
        (CUSTOM, "ab" * 32 + "0"),
        ("0.99.1", DIGEST_A),
        ("v0.99.1;id", DIGEST_A),
        ("v" + "1" * 20, DIGEST_A),
        (DEFAULT_VERSION, DIGEST_A),
    ],
)
def test_pin_custom_refuses_bad_input(server: Server, version: str, digest: str) -> None:
    mp = pin_module()
    with pytest.raises(mp.PinError):
        mp.pin_custom(version, digest, server.layout)
    assert not server.pin_path.exists()


def test_pin_custom_refuses_unsafe_directory_and_pin_path(server: Server) -> None:
    mp = pin_module()
    _directory(server)
    with pytest.raises(mp.PinError):
        mp.pin_custom(CUSTOM, DIGEST_A, server.layout)
    shutil.rmtree(server.pin_path)
    (server.srv / "compose").chmod(0o777)
    with pytest.raises(mp.PinError):
        mp.pin_custom(CUSTOM, DIGEST_A, server.layout)
    (server.srv / "compose").chmod(0o755)
    assert not server.pin_path.exists()


def test_pin_main_accepts_exactly_pin_custom(
    server: Server, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mp = pin_module()
    assert mp.main(["pin-custom", CUSTOM, DIGEST_A], server.layout) == 0
    assert server.pin_path.read_text() == f"{CUSTOM} {DIGEST_A}\n"
    for bad in ([], ["pin-custom"], ["pin-custom", CUSTOM], ["other", CUSTOM, DIGEST_A]):
        assert mp.main(bad, server.layout) == 64
    assert mp.main(["pin-custom", CUSTOM, "nothex"], server.layout) == 68
    assert capsys.readouterr().err


def test_existing_custom_version_requires_admin_pin(
    server: Server, capsys: pytest.CaptureFixture[str]
) -> None:
    """A server already rendered with a custom version keeps its build until a pin exists."""
    mp = pin_module()
    mp.pin_custom(CUSTOM, DIGEST_A, server.layout)
    assert server.render(CUSTOM) == 0
    rendered = {p: p.read_bytes() for p in server.installed}
    server.pin_path.unlink()
    assert server.render(CUSTOM) == 68
    assert "pin-metabase" in capsys.readouterr().err
    assert {p: p.read_bytes() for p in server.installed} == rendered
    assert f"ENV MB_VERSION={CUSTOM}\n" in server.dockerfile


def test_pin_module_reads_nothing_from_the_project_or_environment(
    server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    mp = pin_module()
    monkeypatch.setenv("MB_JAR_SHA256", DIGEST_B)
    (server.srv / "project/.env").write_text(f"MB_JAR_SHA256={DIGEST_B}\n")
    with pytest.raises(mp.PinError):
        mp.digest_for_version(CUSTOM, server.layout)


def test_checksum_template_drift_is_refused(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "pkg"
    shutil.copytree(TEMPLATE, pkg / "templates")
    recipe = pkg / "templates/Dockerfile.metabase"
    recipe.write_text(recipe.read_text().replace(DEFAULT_DIGEST, DIGEST_B))
    monkeypatch.setattr(sc, "resources", SimpleNamespace(files=lambda _name: pkg))
    assert server.render(DEFAULT_VERSION) == 68
    server.assert_untouched()
    recipe.write_text(re.sub(r"--checksum=\S+ ", "", recipe.read_text()))
    assert server.render(DEFAULT_VERSION) == 68
    server.assert_untouched()


def test_packaged_recipe_checks_the_jar_before_any_later_use() -> None:
    """The classic builder ignores ADD --checksum, so a RUN check must follow the ADD."""
    lines = (TEMPLATE / "Dockerfile.metabase").read_text().splitlines()
    add = next(i for i, ln in enumerate(lines) if ln.startswith("ADD --checksum"))
    check = lines.index(f'RUN echo "{DEFAULT_DIGEST}  /app/metabase.jar" | sha256sum -c -')
    assert check == add + 1
    used = [i for i, ln in enumerate(lines) if "metabase.jar" in ln and i not in (add, check)]
    assert all(i > check for i in used)


def test_wrong_digest_in_the_run_check_alone_is_drift(
    server: Server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "pkg"
    shutil.copytree(TEMPLATE, pkg / "templates")
    recipe = pkg / "templates/Dockerfile.metabase"
    original = recipe.read_text()
    monkeypatch.setattr(sc, "resources", SimpleNamespace(files=lambda _name: pkg))
    check = f'RUN echo "{DEFAULT_DIGEST}  /app/metabase.jar" | sha256sum -c -'
    for broken in (
        original.replace(check, check.replace(DEFAULT_DIGEST, DIGEST_B)),
        original.replace(check + "\n", ""),
        original.replace(check, check + "\n" + check),
        original.replace(check, check.replace("sha256sum -c -", "sha256sum")),
    ):
        assert broken != original
        recipe.write_text(broken)
        assert server.render(DEFAULT_VERSION) == 68
        server.assert_untouched()
