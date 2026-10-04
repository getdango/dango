"""tests/unit/test_metabase_password_writers_architecture.py

Architecture guard: only the known modules may write Metabase passwords.
Fails when a new caller of update_metabase_user_password or writer of
UserUpdate(metabase_password_enc=...) appears under dango/.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DANGO_ROOT = Path(__file__).resolve().parents[2] / "dango"

ALLOWED_WRITERS = {
    "dango/auth/metabase_sync.py",
    "dango/cli/commands/auth.py",
    "dango/platform/common/metabase_credential_migration.py",
    "dango/platform/common/metabase_link.py",
}

_CALL = re.compile(r"(?<!def )\bupdate_metabase_user_password\(")
_ENC_WRITE = re.compile(r"UserUpdate\((?:[^()]|\([^()]*\))*?metabase_password_enc\s*=", re.DOTALL)


def _writer_files() -> set[str]:
    found: set[str] = set()
    for path in DANGO_ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _CALL.search(text) or _ENC_WRITE.search(text):
            found.add(path.relative_to(DANGO_ROOT.parent).as_posix())
    return found


@pytest.mark.unit
def test_only_known_modules_write_metabase_passwords() -> None:
    found = _writer_files()
    assert found == ALLOWED_WRITERS, (
        "A new writer of Metabase passwords was added. The admin account's password has ONE "
        "owner (migration/link). Route the change through the owner or justify and add the "
        f"file here (1.0.12 PLAN.md, row 15). Found: {sorted(found)}"
    )
