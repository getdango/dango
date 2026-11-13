"""dango/platform/cloud/backup_destination.py

Where a server's backups go, said truthfully, and which secrets a backup does not hold.

- The Spaces destination (bucket, region) the server-side backup uses.  Deploy writes it to the
  server ``.env`` (``SPACES_BUCKET`` / ``SPACES_REGION``); the values are checked here before
  they are used, because root reads them from a file the service user can write.
- The one line every backup command prints about whether a backup left the server.
- The NAMES (never values) of the variables in the server ``.env`` and the keys in
  ``.dlt/secrets.toml``.  A backup excludes both files by default, so its manifest lists the
  names and a restore tells the user what to re-enter.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tarfile
import tomllib
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

import yaml

from dango.exceptions import CloudError, CloudProvisioningError
from dango.platform.cloud._server_templates import BACKUP_UNIT_MIN_RELEASE
from dango.platform.cloud.project_files import read_project_file

SERVER_PROJECT_DIR = "/srv/dango/project"
#: Local-only deployment state (droplet, firewall and key ids): never part of a backup.
CLOUD_CONFIG_FILE = ".dango/cloud.yml"
#: Start of the line every backup command prints about the destination.
OFFSITE_PREFIX = "Off-site: "
#: Start of the line a server-side restore prints with the secret names the client should show.
RESTORE_SECRETS_PREFIX = "DANGO-RESTORE-SECRETS "
DOWNLOAD_HINT = "dango remote backup download --from-server -o <file>"
DEFAULT_REGION = "nyc1"

_SECRET_PATHS = {".env", ".dlt/secrets.toml"}
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_REGION_RE = re.compile(r"[a-z]{2,6}[0-9]{1,2}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ANY_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,159}")
#: Upper bound for a file read as data and for the names listed.
MAX_TEXT_BYTES = 1024 * 1024
MAX_NAMES = 500


def valid_bucket(name: object) -> bool:
    """True for a Spaces bucket name (3-63 lowercase letters, digits and hyphens)."""
    return isinstance(name, str) and _BUCKET_RE.fullmatch(name) is not None


def valid_region(name: object) -> bool:
    """True for a Spaces region slug such as ``nyc3`` or ``sgp1``."""
    return isinstance(name, str) and _REGION_RE.fullmatch(name) is not None


def validated_destination(bucket: object, region: object, *, source: str) -> dict[str, str]:
    """Return ``{"bucket", "region"}`` after checking both; *source* names where they came from.

    Raises:
        CloudProvisioningError: If the bucket or the region is not a valid name.
    """
    bucket_key, region_key = (
        ("SPACES_BUCKET", "SPACES_REGION") if source == ".env" else ("spaces.bucket", "region")
    )
    if not valid_bucket(bucket):
        raise CloudProvisioningError(
            f"{bucket_key} in the server {source} is not a valid Spaces bucket name "
            "(3-63 lowercase letters, digits and hyphens)"
        )
    region = region or DEFAULT_REGION
    if not valid_region(region):
        raise CloudProvisioningError(
            f"{region_key} in the server {source} is not a valid Spaces region (for example nyc3)"
        )
    return {"bucket": str(bucket), "region": str(region)}


def destination_from_env(values: Mapping[str, str]) -> dict[str, str] | None:
    """The destination named by ``SPACES_BUCKET`` / ``SPACES_REGION`` in *values*, or None.

    Raises:
        CloudProvisioningError: If a value is present but not a valid name.
    """
    bucket = values.get("SPACES_BUCKET")
    if not bucket:
        return None
    return validated_destination(bucket, values.get("SPACES_REGION"), source=".env")


def server_reads_destination(version: str | None) -> bool:
    """True when a server running Dango *version* reads its Spaces destination from ``.env``."""
    from packaging.version import InvalidVersion, Version

    try:
        return Version(version or "").release >= BACKUP_UNIT_MIN_RELEASE
    except InvalidVersion:
        return False


def load_cloud_yml(project_dir: Path) -> dict[str, Any] | None:
    """The server ``.dango/cloud.yml`` of *project_dir* read as data by root, or None.

    The service user controls the file, so a link (the file or a parent directory), a FIFO,
    a non-regular or oversized file reads as absent.  A file that is not valid YAML raises an
    error that carries none of its text; anything that is not a mapping counts as empty.

    Raises:
        CloudProvisioningError: If the file is not valid YAML.
    """
    path = Path(project_dir) / CLOUD_CONFIG_FILE
    if os.path.realpath(path) != os.path.join(os.path.realpath(project_dir), CLOUD_CONFIG_FILE):
        return None
    text = _read_text_nofollow(path)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise CloudProvisioningError("server .dango/cloud.yml is not valid YAML") from None
    return data if isinstance(data, dict) else {}


def offsite_line(bucket: str | None, *, verified: bool = True) -> str:
    """The plain statement of whether a backup left the server."""
    if not bucket:
        return f"{OFFSITE_PREFIX}NO: backups exist only on this server and are lost with it"
    if not verified:
        return f"{OFFSITE_PREFIX}unverified (Spaces {bucket}): the upload could not be confirmed"
    return f"{OFFSITE_PREFIX}yes (Spaces {bucket})"


# ---------------------------------------------------------------------------
# Secret names (never values)
# ---------------------------------------------------------------------------


def _closes_quote(text: str, quote: str, start: int) -> bool:
    """True if *text* has an unescaped *quote* at or after *start*."""
    index = start
    while index < len(text):
        if text[index] == "\\":
            index += 2
            continue
        if text[index] == quote:
            return True
        index += 1
    return False


def env_secret_names(text: str) -> list[str]:
    """Variable names in ``.env`` *text*, sorted; values are never returned.

    Lines end at ``\\n`` only (a form feed or U+2028 inside a value is part of the value).
    A quoted value that runs over several lines, ending at its first unescaped closing quote,
    is passed over as a whole, so a line inside it is never taken for a name.  Anything that
    is not a plain identifier is dropped.
    """
    names: set[str] = set()
    open_quote = ""
    for line in text[:MAX_TEXT_BYTES].split("\n"):
        line = line.rstrip("\r")
        if open_quote:
            if _closes_quote(line, open_quote, 0):
                open_quote = ""
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ('"', "'") and not _closes_quote(value, value[0], 1):
            open_quote = value[0]
        if _ENV_NAME_RE.fullmatch(key):
            names.add(key)
    return sorted(names)[:MAX_NAMES]


def toml_secret_names(text: str) -> list[str]:
    """Dotted key paths of the values in ``secrets.toml`` *text*, sorted; no values."""
    try:
        data = tomllib.loads(text[:MAX_TEXT_BYTES])
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    names: set[str] = set()

    def walk(node: Any, path: str) -> None:
        """Collect the dotted path of every leaf value under *node*."""
        if isinstance(node, dict):
            for key, child in node.items():
                walk(child, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list) and any(isinstance(item, dict) for item in node):
            for item in node:
                walk(item, path)
        elif path and _ANY_NAME_RE.fullmatch(path):
            names.add(path)

    walk(data, "")
    return sorted(names)[:MAX_NAMES]


def server_secret_names(ssh: Any) -> list[str]:
    """Names in the server ``.env`` and ``.dlt/secrets.toml``, read as the service user.

    A file that cannot be read contributes nothing.
    """
    names: set[str] = set()
    for rel, extract in ((".env", env_secret_names), (".dlt/secrets.toml", toml_secret_names)):
        try:
            text = read_project_file(ssh, f"{SERVER_PROJECT_DIR}/{rel}")
        except (CloudError, OSError):  # best effort: an unreadable file lists nothing
            continue
        names.update(extract(text))
    return sorted(names)[:MAX_NAMES]


def _read_text_nofollow(path: Path, max_bytes: int = MAX_TEXT_BYTES) -> str | None:
    """Return a regular file's text without following a link or blocking on a FIFO; else None."""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        return os.read(fd, max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def local_secret_names(project_dir: Path) -> list[str]:
    """Names in a project's ``.env`` and ``.dlt/secrets.toml``, for a process running as root.

    Read as data: a link is never followed (the file or a parent directory), and only a
    regular file of bounded size is read.
    """
    real_root = os.path.realpath(project_dir)
    names: set[str] = set()
    for rel, extract in ((".env", env_secret_names), (".dlt/secrets.toml", toml_secret_names)):
        path = Path(project_dir) / rel
        if os.path.realpath(path) != os.path.join(real_root, rel):
            continue
        text = _read_text_nofollow(path)
        if text is not None:
            names.update(extract(text))
    return sorted(names)[:MAX_NAMES]


def clean_names(raw: object) -> list[str] | None:
    """The valid names in a manifest's ``secret_names`` value; None when it is not a list."""
    if not isinstance(raw, list):
        return None
    return [n for n in raw if isinstance(n, str) and _ANY_NAME_RE.fullmatch(n)][:MAX_NAMES]


def archive_secrets_info(source: bytes | Path) -> tuple[list[str] | None, bool]:
    """``(secret names, secrets included)`` from a backup archive's manifest.

    The names are None when the archive has no manifest or an older one that lists none.
    Entries that are not plain names are dropped.  The manifest is read as data, never
    used to decide what a restore writes.
    """
    try:
        tf = (
            tarfile.open(fileobj=BytesIO(source), mode="r:gz")
            if isinstance(source, bytes)
            else tarfile.open(source, mode="r:gz")
        )
        with tf:
            for member in tf:
                parts = member.name.split("/")
                if len(parts) != 2 or parts[1] != "manifest.json" or not member.isfile():
                    continue
                handle = tf.extractfile(member)
                if handle is None or member.size > MAX_TEXT_BYTES:
                    return None, False
                manifest = json.loads(handle.read())
                if not isinstance(manifest, dict):
                    return None, False
                if "secrets_included" in manifest:
                    included = bool(manifest["secrets_included"])
                else:  # an older manifest: the file list says whether secrets were copied
                    files = manifest.get("files")
                    paths = (
                        {f.get("path") for f in files if isinstance(f, dict)}
                        if isinstance(files, list)
                        else set()
                    )
                    included = bool(paths & _SECRET_PATHS)
                return clean_names(manifest.get("secret_names")), included
    except (OSError, EOFError, tarfile.TarError, ValueError):
        return None, False
    return None, False


def reenter_message(names: list[str] | None) -> str:
    """What to tell the user after a backup without secrets is downloaded or restored."""
    if names is None:
        return (
            "secrets are not in this backup, and it was made by an older Dango that does not "
            "list their names; re-enter your .env variables and .dlt/secrets.toml keys"
        )
    if not names:
        return "secrets are not in this backup; no secret names were set on the server"
    return f"secrets are not in this backup; re-enter: {', '.join(names)}"


def secrets_marker_line(archive: Path) -> str | None:
    """The line a server-side restore prints so the client can name what to re-enter.

    None when the archive holds the secrets (nothing to re-enter) or cannot be read.
    """
    if not archive.is_file():
        return None
    names, included = archive_secrets_info(archive)
    if included:
        return None
    return RESTORE_SECRETS_PREFIX + json.dumps(names)
