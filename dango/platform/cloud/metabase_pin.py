"""dango/platform/cloud/metabase_pin.py

Which SHA-256 the server build accepts for the Metabase jar.

The default version uses the digest compiled in below.  Any other version needs a root-owned
record in ``compose/metabase-jar.pin`` (``VERSION SHA256``), written by an administrator
through the helper's ``pin-metabase`` verb.  Nothing here reads the project tree, the
environment or the current directory: the project Dockerfile is writable by the service
user, so it can never supply a digest.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from typing import TYPE_CHECKING

from dango.platform.cloud.compose_helper import HELPER_PATH

if TYPE_CHECKING:
    from dango.platform.cloud.server_compose import Layout

DEFAULT_MB_VERSION = "v0.62.18"
#: SHA-256 of the v0.62.18 jar: equal for the exact-version download and for the jar in the
#: publisher's ``metabase/metabase:v0.62.18`` image (two official artifacts; the publisher
#: does not post a checksum).  Must equal the digest on the packaged recipe's ``ADD`` line.
DEFAULT_MB_DIGEST = "4cc9c801ac616e4b5093c4906cb59f6e8f819f620c65d94ca4469ed87034a2e5"
PIN_NAME = "metabase-jar.pin"

VERSION_RE = re.compile(r"v[0-9][0-9.]{0,18}")
DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_RECORD_RE = re.compile(rb"(v[0-9][0-9.]{0,18}) ([0-9a-f]{64})\n")
_MAX_RECORD = 128


class PinError(Exception):
    """The digest for a version cannot be trusted or stored; the message says what to do."""


def _recovery(version: str) -> str:
    return f"as root run: {HELPER_PATH} pin-metabase {version} <sha256-of-the-jar>"


def _check_dir(fd: int, layout: Layout) -> None:
    st = os.fstat(fd)
    if st.st_uid != layout.owner_uid or st.st_mode & 0o022:
        raise PinError(f"{layout.compose} is not a root-owned, private directory")


def _open_dir(layout: Layout) -> int:
    try:
        fd = os.open(layout.compose, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise PinError(f"{layout.compose} is missing or not a plain directory") from None
    try:
        _check_dir(fd, layout)
    except PinError:
        os.close(fd)
        raise
    return fd


def digest_for_version(version: str, layout: Layout) -> str:
    """The SHA-256 the jar for *version* must have; :class:`PinError` when there is none."""
    if not VERSION_RE.fullmatch(version):
        raise PinError("bad Metabase version")
    if version == DEFAULT_MB_VERSION:
        return DEFAULT_MB_DIGEST
    missing = PinError(f"Metabase {version} has no administrator pin; {_recovery(version)}")
    dfd = _open_dir(layout)
    try:
        try:
            fd = os.open(
                PIN_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY,
                dir_fd=dfd,
            )
        except FileNotFoundError:
            raise missing from None
        except OSError:
            raise PinError(f"the Metabase pin file is unsafe; {_recovery(version)}") from None
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != layout.owner_uid
        or st.st_mode & 0o022
        or st.st_size > _MAX_RECORD
    ):
        os.close(fd)
        raise PinError(f"the Metabase pin file is unsafe; {_recovery(version)}")
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(_MAX_RECORD + 1)
    return parse_pin_record(data, version)


def parse_pin_record(data: bytes, version: str) -> str:
    """The SHA-256 *data* (the pin file's bytes) records for *version*; :class:`PinError` otherwise.

    The one reader of the record format: the server's render uses it, and so does the upgrade
    pre-flight, so that both accept and refuse exactly the same files.
    """
    if len(data) > _MAX_RECORD:
        raise PinError(f"the Metabase pin file is unsafe; {_recovery(version)}")
    found = _RECORD_RE.fullmatch(data)
    if found is None:
        raise PinError(f"the Metabase pin file is malformed; {_recovery(version)}")
    if found.group(1).decode("ascii") != version:
        raise PinError(f"Metabase {version} has no administrator pin; {_recovery(version)}")
    return found.group(2).decode("ascii")


def pin_custom(version: str, digest: str, layout: Layout) -> None:
    """Store the record for *version* atomically as a private file in the compose directory."""
    digest = digest.lower()
    if not VERSION_RE.fullmatch(version):
        raise PinError("bad Metabase version")
    if version == DEFAULT_MB_VERSION:
        raise PinError(f"{version} is the default version and uses its built-in digest")
    if not DIGEST_RE.fullmatch(digest):
        raise PinError("the digest must be 64 hexadecimal characters")
    record = f"{version} {digest}\n".encode("ascii")
    tmp = f".{PIN_NAME}.{secrets.token_hex(8)}.tmp"
    dfd = _open_dir(layout)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                if os.fstat(handle.fileno()).st_uid != layout.owner_uid:
                    raise PinError("cannot create a file owned by the expected user")
                handle.write(record)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, PIN_NAME, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
    except OSError as exc:
        raise PinError(f"cannot store the pin: {exc.strerror or 'file error'}") from None
    finally:
        os.close(dfd)


def main(argv: list[str], layout: Layout | None = None) -> int:
    """Entry point for ``pin-custom VERSION SHA256``; returns the helper's exit status."""
    from dango.platform.cloud.server_compose import EXIT_PIN, EXIT_USAGE, Layout

    if len(argv) != 3 or argv[0] != "pin-custom":
        print("dango-compose: usage: pin-custom VERSION SHA256", file=sys.stderr)
        return EXIT_USAGE
    try:
        pin_custom(argv[1], argv[2], layout or Layout())
    except PinError as exc:
        print(f"dango-compose: pin-metabase: {exc}", file=sys.stderr)
        return EXIT_PIN
    print(f"dango-compose: pinned Metabase {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
