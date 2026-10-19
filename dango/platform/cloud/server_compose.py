"""dango/platform/cloud/server_compose.py

Server-side render for the root-owned compose helper (not yet enabled on any server).

Run by the helper's ``render`` verb, as root, with ``python -I``::

    python -I -m dango.platform.cloud.server_compose MB_PORT DOCS_PORT MB_VERSION NAME

The project name and id come only from the root-owned pin files; the arguments are
checked again here.  Plugin jars are read through a fixed reader run as the service
user (bounded in size and time, killed as a process group), staged without the lock,
and moved into a root-only directory together with the compose file and the Metabase
build files while the shared lock is held.  Nothing here reads the environment, the
current directory or any file outside the server layout.
"""

from __future__ import annotations

import fcntl
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

from jinja2 import Environment, PackageLoader, select_autoescape

from dango.platform.cloud.compose_helper import DOCS_ID, LOCK_DIR, METABASE_ID, SRV_DIR

EXIT_USAGE = 64
EXIT_STAGE = 65
EXIT_NAME = 66
EXIT_IDS = 67
EXIT_BUSY = 75

_JAR_RE = re.compile(r"[A-Za-z0-9._-]+\.jar")
_NAME_RE = re.compile(r"dango-[0-9a-f]{8}")
_ID_RE = re.compile(r"[0-9a-f]{8}[0-9a-f-]{0,28}")
_VERSION_RE = re.compile(r"v[0-9][0-9.]{0,18}")
_PORT_RE = re.compile(r"[1-9][0-9]{0,4}")
_MIB = 1024 * 1024

# Fixed programs run as the service user (``python -I -c``): list names; stream one jar.
_LISTER = (
    "import os,sys\n"
    "try:\n    print('\\n'.join(sorted(os.listdir(sys.argv[1]))))\n"
    "except FileNotFoundError:\n    pass\n"
)
_READER = (
    "import os,stat,sys\n"
    "d=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)\n"
    "f=os.open(sys.argv[2],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_NOCTTY,dir_fd=d)\n"
    "st=os.fstat(f)\n"
    "if not stat.S_ISREG(st.st_mode) or st.st_size>int(sys.argv[3]):\n    sys.exit(3)\n"
    "o=sys.stdout.buffer\n"
    "while True:\n    b=os.read(f,1048576)\n    if not b:\n        break\n    o.write(b)\n"
    "o.flush()\n"
)


class RenderError(Exception):
    """A render failure; ``code`` is the helper's exit status."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Layout:
    """Server paths and limits (a parameter so tests can use a temp tree)."""

    srv: str = SRV_DIR
    lock_dir: str = LOCK_DIR
    owner_uid: int = 0
    lock_wait: float = 30.0
    reader_prefix: tuple[str, ...] = ("runuser", "-u", "dango", "--", f"{SRV_DIR}/venv/bin/python")
    jar_cap: int = 256 * _MIB
    jar_timeout: float = 60.0
    total_timeout: float = 300.0
    max_jars: int = 10

    @property
    def compose(self) -> str:
        """Compose project directory (root-owned)."""
        return f"{self.srv}/compose"

    @property
    def build(self) -> str:
        """Metabase build context."""
        return f"{self.srv}/compose/build"

    @property
    def metabase(self) -> str:
        """Root-only directory (0700) holding the plugin copies."""
        return f"{self.srv}/metabase"

    @property
    def plugins(self) -> str:
        """Root-owned copy of the plugin jars, bind-mounted into Metabase."""
        return f"{self.srv}/metabase/plugins"

    @property
    def project_plugins(self) -> str:
        """The service user's plugin directory (read through the fixed reader)."""
        return f"{self.srv}/project/metabase-plugins"


def quote_name(name: str) -> str:
    """The name as a double-quoted YAML scalar; refuses what cannot be quoted verbatim."""
    if not 1 <= len(name) <= 200:
        raise RenderError(EXIT_USAGE, "bad project name length")
    for ch in name:
        if (
            ch in '\\"$'
            or unicodedata.category(ch)[0] == "C"
            or unicodedata.category(ch) in ("Zl", "Zp")
        ):
            raise RenderError(
                EXIT_NAME, "project name has a character the server render cannot quote"
            )
    return f'"{name}"'


def acquire_lock(lock_dir: str, owner_uid: int, wait: float) -> int:
    """Take the shared lock under the same rules as the helper's ``lockdir``/``flock``.

    The directory must be owned by *owner_uid* with mode 0700 and not be a link; the
    file is created 0600 and must be owned the same way.  Raises ``RenderError(75)``
    when the lock is still held after *wait* seconds.
    """
    unsafe = RenderError(EXIT_USAGE, f"{lock_dir} unsafe")
    try:
        os.mkdir(lock_dir, 0o700)
    except FileExistsError:
        pass
    try:
        dfd = os.open(lock_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise unsafe from None
    try:
        dst = os.fstat(dfd)
        if dst.st_uid != owner_uid or stat.S_IMODE(dst.st_mode) != 0o700:
            raise unsafe
        previous = os.umask(0o077)
        try:
            fd = os.open("lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        except OSError:
            raise unsafe from None
        finally:
            os.umask(previous)
    finally:
        os.close(dfd)
    fst = os.fstat(fd)
    if (
        not stat.S_ISREG(fst.st_mode)
        or fst.st_uid != owner_uid
        or stat.S_IMODE(fst.st_mode) != 0o600
    ):
        os.close(fd)
        raise unsafe
    end = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= end:
                os.close(fd)
                raise RenderError(
                    EXIT_BUSY, "busy (another Dango Docker operation is running)"
                ) from None
            time.sleep(0.05)


def _read_pinned_file(path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise RenderError(EXIT_USAGE, "project is not pinned") from None
    with os.fdopen(fd, "rb") as handle:
        return handle.read(256).decode("ascii", "replace").strip()


def read_pinned(layout: Layout) -> tuple[str, str]:
    """Return (compose project name, project id) from the root-owned pin files."""
    name = _read_pinned_file(f"{layout.compose}/project")
    ident = _read_pinned_file(f"{layout.compose}/project-id")
    if (
        not _NAME_RE.fullmatch(name)
        or not _ID_RE.fullmatch(ident)
        or ident[:8] != name[len("dango-") :]
    ):
        raise RenderError(EXIT_USAGE, "pinned project files are inconsistent")
    return name, ident


def _stream(argv: list[str], write: Callable[[bytes], object], cap: int, timeout: float) -> None:
    """Run *argv* in its own process group and pass its stdout to *write*.

    The group is killed as soon as more than *cap* bytes arrive, the time runs out, or
    the program exits non-zero.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    end = time.monotonic() + timeout
    total = 0
    try:
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise RenderError(EXIT_STAGE, "timed out reading plugin jars")
            ready, _, _ = select.select([proc.stdout], [], [], min(left, 0.5))
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), _MIB)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise RenderError(EXIT_STAGE, "plugin jar is larger than the limit")
            write(chunk)
        if proc.wait(timeout=max(1.0, end - time.monotonic())) != 0:
            raise RenderError(EXIT_STAGE, "plugin jar is not a readable regular file")
    except subprocess.TimeoutExpired:
        raise RenderError(EXIT_STAGE, "timed out reading plugin jars") from None
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        proc.stdout.close()


def list_jars(layout: Layout) -> list[str]:
    """Plugin jar names in the project (read as the service user); bad names are refused."""
    out = bytearray()
    argv = [*layout.reader_prefix, "-I", "-c", _LISTER, layout.project_plugins]
    _stream(argv, out.extend, 65536, 30.0)
    jars = [n for n in out.decode("utf-8", "replace").splitlines() if n.endswith(".jar")]
    for jar in jars:
        if not _JAR_RE.fullmatch(jar):
            raise RenderError(EXIT_STAGE, f"unsupported plugin file name: {jar!r}")
    if len(jars) > layout.max_jars:
        raise RenderError(EXIT_STAGE, f"more than {layout.max_jars} plugin jars")
    return jars


def stage_jars(layout: Layout, jars: list[str], stage: str) -> None:
    """Copy each jar into *stage* through the fixed reader, within the size and time caps."""
    started = time.monotonic()
    for jar in jars:
        left = layout.total_timeout - (time.monotonic() - started)
        if left <= 0:
            raise RenderError(EXIT_STAGE, f"timed out reading plugin jar {jar}")
        argv = [
            *layout.reader_prefix,
            "-I",
            "-c",
            _READER,
            layout.project_plugins,
            jar,
            str(layout.jar_cap),
        ]
        fd = os.open(f"{stage}/{jar}", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, "wb") as out:
                _stream(argv, out.write, layout.jar_cap, min(layout.jar_timeout, left))
        except RenderError as exc:
            raise RenderError(exc.code, f"{exc} ({jar})") from None


def _write_tmp(path: str, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        os.fchmod(handle.fileno(), mode)


def render_compose(layout: Layout, mb_port: int, docs_port: int, name_q: str, id_q: str) -> str:
    """The server compose file (template ``server`` branch: absolute paths, long bind syntax)."""
    template = Environment(
        loader=PackageLoader("dango", "templates"), autoescape=select_autoescape(default=False)
    ).get_template("docker-compose.yml.j2")
    return template.render(
        server=True,
        metabase_port=mb_port,
        dbt_docs_port=docs_port,
        project_name_q=name_q,
        project_id_q=id_q,
        build_dir=layout.build,
        data_dir=f"{layout.srv}/project/data",
        dbt_target_dir=f"{layout.srv}/project/dbt/target",
        plugins_dir=layout.plugins,
        docs_uid=DOCS_ID,
        docs_gid=DOCS_ID,
    )


def render_dockerfile(version: str) -> str:
    """The packaged Metabase Dockerfile with ``MB_VERSION`` replaced (exactly one occurrence)."""
    text = (resources.files("dango") / "templates" / "Dockerfile.metabase").read_text(
        encoding="utf-8"
    )
    out, count = re.subn(r"(?m)^ENV MB_VERSION=.*$", f"ENV MB_VERSION={version}", text)
    if count != 1:
        raise RenderError(EXIT_STAGE, "packaged Dockerfile has no single MB_VERSION line")
    out = _swap(out, "groupadd -r metabase", f"groupadd -r -g {METABASE_ID} metabase")
    return _swap(out, "useradd -r -g metabase", f"useradd -r -u {METABASE_ID} -g metabase")


def render_entrypoint() -> bytes:
    """The packaged entrypoint with the fixed Metabase ids in place of the names."""
    text = (resources.files("dango") / "templates" / "entrypoint.sh").read_text(encoding="utf-8")
    ids = f"{METABASE_ID}:{METABASE_ID}"
    text = _swap(text, "chown -R metabase:metabase /app/plugins", f"chown -R {ids} /app/plugins")
    text = _swap(
        text, "\n# Drop privileges", f"\nchown -R {ids} /metabase-data\n\n# Drop privileges"
    )
    return _swap(text, "gosu metabase", f"gosu {ids}").encode()


def _swap(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RenderError(EXIT_STAGE, "packaged Metabase build files changed unexpectedly")
    return text.replace(old, new)


def _getent(database: str, key: str) -> str:
    try:
        done = subprocess.run(
            ["getent", database, key],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RenderError(EXIT_IDS, "cannot check the host user and group ids") from None
    if done.returncode not in (0, 2):
        raise RenderError(EXIT_IDS, "cannot check the host user and group ids")
    return done.stdout if done.returncode == 0 else ""


def check_container_ids() -> None:
    """Refuse when a host user or group already uses a container id, or docker's gid is one."""
    for ident in (METABASE_ID, DOCS_ID):
        for database in ("passwd", "group"):
            if _getent(database, str(ident)).strip():
                raise RenderError(EXIT_IDS, f"container id {ident} is already used on this host")
    fields = _getent("group", "docker").strip().split(":")
    if len(fields) >= 3 and fields[2] in (str(METABASE_ID), str(DOCS_ID)):
        raise RenderError(EXIT_IDS, "docker group id is a container id")


def run(argv: list[str], layout: Layout) -> None:
    """Validate, render and install; raises :class:`RenderError`."""
    if len(argv) != 4:
        raise RenderError(EXIT_USAGE, "usage: render MB_PORT DOCS_PORT MB_VERSION NAME")
    mb_s, docs_s, version, name = argv
    if not (_PORT_RE.fullmatch(mb_s) and _PORT_RE.fullmatch(docs_s)):
        raise RenderError(EXIT_USAGE, "bad port")
    mb_port, docs_port = int(mb_s), int(docs_s)
    if not (1024 <= mb_port <= 65535 and 1024 <= docs_port <= 65535) or mb_port == docs_port:
        raise RenderError(EXIT_USAGE, "bad port")
    if not _VERSION_RE.fullmatch(version):
        raise RenderError(EXIT_USAGE, "bad version")
    name_q = quote_name(name)
    check_container_ids()
    _, ident = read_pinned(layout)
    compose = render_compose(layout, mb_port, docs_port, name_q, f'"{ident}"')
    dockerfile = render_dockerfile(version)
    entrypoint = render_entrypoint()

    jars = list_jars(layout)
    os.makedirs(layout.plugins, mode=0o755, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".stage-", dir=layout.metabase)
    tmps = [
        (f"{layout.compose}/.docker-compose.yml.tmp", f"{layout.compose}/docker-compose.yml"),
        (f"{layout.build}/.Dockerfile.metabase.tmp", f"{layout.build}/Dockerfile.metabase"),
        (f"{layout.build}/.entrypoint.sh.tmp", f"{layout.build}/entrypoint.sh"),
    ]
    try:
        stage_jars(layout, jars, stage)
        _write_tmp(tmps[0][0], compose.encode(), 0o644)
        _write_tmp(tmps[1][0], dockerfile.encode(), 0o644)
        _write_tmp(tmps[2][0], entrypoint, 0o644)
        lock_fd = acquire_lock(layout.lock_dir, layout.owner_uid, layout.lock_wait)
        try:
            for jar in jars:
                os.replace(f"{stage}/{jar}", f"{layout.plugins}/{jar}")
            for tmp, final in tmps:
                os.replace(tmp, final)
            for entry in os.scandir(layout.plugins):
                if (
                    _JAR_RE.fullmatch(entry.name)
                    and entry.name not in jars
                    and entry.is_file(follow_symlinks=False)
                ):
                    os.unlink(entry.path)
        finally:
            os.close(lock_fd)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        for tmp, _ in tmps:
            if os.path.lexists(tmp):
                os.unlink(tmp)


def main(argv: list[str], layout: Layout | None = None) -> int:
    """Entry point; returns the helper's exit status."""
    try:
        run(argv, layout or Layout())
    except RenderError as exc:
        print(f"dango-compose: render: {exc}", file=sys.stderr)
        return exc.code
    except OSError as exc:
        print(f"dango-compose: render: {exc.strerror or 'file error'}", file=sys.stderr)
        return EXIT_STAGE
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
