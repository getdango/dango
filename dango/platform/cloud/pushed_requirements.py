"""dango/platform/cloud/pushed_requirements.py

Root installs project ``requirements.txt`` files only from the pusher's local copies.

The server's copies are writable by the ``dango`` service account, so they are
never read; lines that would make root build local or credential-bearing sources
are refused.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from dango.logging import get_logger

if TYPE_CHECKING:
    from dango.platform.cloud.ssh import SSHManager

_logger = get_logger(__name__)

_SAFE_REMOTE_DIR = re.compile(r"/[A-Za-z0-9._/-]+")
REQUIREMENT_FILES = ("requirements.txt", "scripts/requirements.txt")
_NESTED_OR_EDITABLE = re.compile(
    r"^(-e|--editable|-r|--requirement|-c|--constraint)(\s|=|$)|^-[erc]\S", re.I
)
_URL_OPTION = re.compile(r"^(-i|--index-url|--extra-index-url|-f|--find-links)(?:\s+|=)(\S+)", re.I)
_WEB_URL = re.compile(r"^https?://", re.I)
_URL_CREDENTIALS = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s@]+@", re.I)
_SSH_GIT_USER = re.compile(r"^[a-z+]*ssh://git@", re.I)
_LOCAL_ARCHIVE = re.compile(r"\.(whl|tar\.gz|zip|tgz|tar\.bz2)$", re.I)


def _strip_comment(line: str) -> str:
    return re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()


_COMMENT_START = re.compile(r"(^|\s+)#.*$")


def _join_lines(lines: list[str]) -> list[str]:
    """Join backslash continuations exactly as pip's ``join_lines`` does.

    A line continues only if it ends with a backslash and is not a comment line;
    continued pieces have their leading and trailing backslashes stripped.
    """
    joined: list[str] = []
    pending: list[str] = []
    for line in lines:
        is_comment = _COMMENT_START.match(line) is not None
        if not line.endswith("\\") or is_comment:
            if is_comment:
                line = " " + line
            if pending:
                pending.append(line)
                joined.append("".join(pending))
                pending = []
            else:
                joined.append(line)
        else:
            pending.append(line.strip("\\"))
    if pending:
        joined.append("".join(pending))
    return joined


def _redact(line: str) -> str:
    return re.sub(r"(://)[^/\s@]+@", r"\1***@", line)


def check_requirements(text: str) -> list[str]:
    """Return the lines root must not install (credentials redacted); ``[]`` if all are fine.

    Refused: editable installs, nested ``-r``/``-c`` files, ``file:`` and local or
    relative paths, local archives, non-http(s) index/find-links URLs, and URLs
    carrying credentials. Backslash continuations are joined first, as pip does.
    """
    refused: list[str] = []
    for raw in _join_lines(text.splitlines()):
        line = _strip_comment(raw)
        if not line:
            continue
        url_option = _URL_OPTION.match(line)
        if url_option:
            target = url_option.group(2)
            if not _WEB_URL.match(target) or _URL_CREDENTIALS.search(target):
                refused.append(_redact(line))
            continue
        if re.search(r"\s(-e|--editable|-r|--requirement|-c|--constraint)(\s|=|$)", line):
            refused.append(_redact(line))
            continue
        spec = line.split(";", 1)[0].split(" --hash", 1)[0].strip()
        url = spec.split("@", 1)[1].strip() if " @ " in spec else spec
        local = (
            spec.startswith((".", "/", "~"))
            or "file:" in spec.lower()
            or ("://" not in url and ("/" in url or _LOCAL_ARCHIVE.search(url) is not None))
        )
        credentials = _URL_CREDENTIALS.search(spec) and not _SSH_GIT_USER.match(url)
        if _NESTED_OR_EDITABLE.match(line) or local or credentials:
            refused.append(_redact(line))
    return refused


def install_pushed_requirements(
    ssh: SSHManager,
    local_project_root: Path,
    pip: str,
    warnings: list[str],
) -> list[str]:
    """Install the local requirements files with *pip*, as root, from private uploads.

    The server's copies are never read: they are writable by ``dango``. Returns
    the labels of the files installed.
    """
    installed: list[str] = []
    for rel in REQUIREMENT_FILES:
        local = local_project_root / rel
        if not local.is_file():
            continue
        text = local.read_text(encoding="utf-8")
        refused = check_requirements(text)
        if refused:
            warnings.append(
                f"{rel} not installed on the server: root installs only index packages and "
                f"remote URLs without credentials; refused: {'; '.join(refused)}"
            )
            continue
        made = ssh.exec_command("mktemp -d -t dango-req-XXXXXXXXXX")
        workdir = made.stdout.strip() if made.success else ""
        if not _SAFE_REMOTE_DIR.fullmatch(workdir):
            warnings.append(f"{rel} not installed: could not create a private directory")
            continue
        try:
            remote = f"{workdir}/requirements.txt"
            ssh.write_remote_file(remote, text, mode=0o600)
            result = ssh.exec_command(f"{pip} install --no-input -r {remote}", timeout=300)
        finally:
            ssh.exec_command(f"rm -rf {workdir}")  # cleanup: silent OK
        if result.success:
            installed.append(rel)
            _logger.info("requirements_installed", path=rel)
        else:
            warnings.append(f"pip install -r {rel} failed: {result.stderr.strip()}")
    return installed
