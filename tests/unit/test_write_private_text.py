"""tests/unit/test_write_private_text.py

write_private_text (atomic, owner-only replace) and the set_env_text / unset_env_text helpers for .env text.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest

from dango.utils.private_files import write_private_text
from dango.utils.project_env import (
    parse_env_text,
    read_env,
    set_env_text,
    set_env_value,
    unset_env_text,
)

pytestmark = pytest.mark.unit


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_creates_the_file_with_the_text_and_no_leftover(tmp_path: Path) -> None:
    target = tmp_path / "secrets.toml"
    write_private_text(target, "a = 'é'\nb = 1\n")
    assert target.read_bytes() == "a = 'é'\nb = 1\n".encode()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["secrets.toml"]
    if os.name == "posix":
        assert _mode(target) == 0o600


def test_replaces_content_and_tightens_a_loose_file(tmp_path: Path) -> None:
    target = tmp_path / "secrets.toml"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o644)
    write_private_text(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    if os.name == "posix":
        assert _mode(target) == 0o600


def test_line_endings_are_lf(tmp_path: Path) -> None:
    target = tmp_path / "f"
    write_private_text(target, "a\nb\n")
    assert b"\r" not in target.read_bytes()


def test_a_failed_replace_keeps_the_old_file_and_removes_the_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f"
    target.write_text("old\n", encoding="utf-8")

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        write_private_text(target, "new\n")
    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_a_failed_fsync_keeps_the_old_file_and_removes_the_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f"
    target.write_text("old\n", encoding="utf-8")

    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(OSError, match="disk full"):
        write_private_text(target, "new\n")
    assert target.read_text(encoding="utf-8") == "old\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f"]


def test_a_missing_folder_raises_and_creates_nothing(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        write_private_text(tmp_path / "missing" / "f", "x")
    assert list(tmp_path.iterdir()) == []


# --- set_env_text / unset_env_text -----------------------------------------------------------


@pytest.mark.parametrize("mode", ["always", "auto"])
def test_set_env_text_matches_what_set_env_value_writes(tmp_path: Path, mode: str) -> None:
    text = "# c\n\nA=1\nB='two words'\n"
    env = tmp_path / ".env"
    env.write_text(text, encoding="utf-8")
    for key, value in (("B", "it's \\ fine"), ("NEW", "x y"), ("A", "abc"), ("Z", "")):
        set_env_value(env, key, value, quote_mode=mode)
        text = set_env_text(text, key, value, quote_mode=mode)
    assert text == env.read_text(encoding="utf-8")
    assert parse_env_text(text) == read_env(tmp_path)


def test_set_env_text_appends_after_a_missing_final_newline() -> None:
    assert set_env_text("A=1", "B", "x", quote_mode="auto") == "A=1\nB=x\n"
    assert set_env_text("", "B", "x y") == "B='x y'\n"


def test_set_env_text_replaces_in_place_and_keeps_order_and_crlf() -> None:
    text = "# top\r\nA=1\r\nB=2\r\nC=3\r\n"
    out = set_env_text(text, "B", "9", quote_mode="auto")
    assert out == "# top\r\nA=1\r\nB=9\nC=3\r\n"


def test_set_env_text_rejects_an_unknown_quote_mode() -> None:
    with pytest.raises(ValueError, match="quote_mode"):
        set_env_text("", "A", "1", quote_mode="sometimes")


def test_unset_env_text_removes_only_that_key() -> None:
    text = "# c\nA=1\n\nB='x y'\nA=again\n"
    assert unset_env_text(text, "A") == "# c\n\nB='x y'\n"
    assert unset_env_text(text, "NOPE") == text


@pytest.mark.parametrize(
    ("value", "line"),
    [
        ("new_value", "A=new_value\n"),
        ("https://h.example/p", "A=https://h.example/p\n"),
        ("a b", "A='a b'\n"),
        ("a#b", "A='a#b'\n"),
        ("$x", "A='$x'\n"),
        ("a\\b", "A='a\\\\b'\n"),
        ("", "A=''\n"),
    ],
)
def test_auto_quoting_leaves_only_safe_values_bare(value: str, line: str) -> None:
    assert set_env_text("", "A", value, quote_mode="auto") == line
    assert parse_env_text(line)["A"] == value
