"""tests/unit/test_project_env.py

The one safe project `.env` loader: same results as python-dotenv for valid UTF-8 files, and one
friendly `EnvFileError` (names the file, says plain UTF-8) for every file that cannot be read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from dango.utils.project_env import (
    EnvFileError,
    describe_env_read_error,
    load_env,
    parse_env_text,
    read_env,
)
from dotenv import dotenv_values, load_dotenv

pytestmark = pytest.mark.unit

# What the WP-35 reviewer's battery covered: export, quotes, interpolation, duplicates, CRLF, BOM.
BATTERY: dict[str, bytes] = {
    "export": b"export WP37_A=one\nWP37_B=two\n",
    "quotes": b"WP37_A='single value'\nWP37_B=\"double value\"\nWP37_C=bare # comment\n",
    "interpolation": b"WP37_A=base\nWP37_B=${WP37_A}/sub\nWP37_C=${WP37_MISSING:-fallback}\n",
    "duplicates": b"WP37_A=first\nWP37_A=second\n",
    "crlf": b"WP37_A=one\r\nWP37_B=two\r\n",
    "bom": b"\xef\xbb\xbfWP37_A=one\nWP37_B=two\n",
    "bom_crlf": b"\xef\xbb\xbfWP37_A=one\r\nWP37_B=two\r\n",
    "empty_and_bare": b"WP37_A=\nWP37_B\nWP37_C=3\n",
    "comments_blank": b"# header\n\nWP37_A=1\n   \n# tail\n",
    "multiline": b'WP37_A="line one\nline two"\nWP37_B=2\n',
    "non_ascii": "WP37_A=café — über\n".encode(),
    "existing_name": b"WP37_PRESET=from-file\nWP37_A=1\n",
}
NAMES = ("WP37_A", "WP37_B", "WP37_C", "WP37_PRESET", "WP37_MISSING")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)


def _project(tmp_path: Path, data: bytes | None) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    if data is not None:
        (root / ".env").write_bytes(data)
    return root


@pytest.mark.parametrize("name", sorted(BATTERY))
def test_read_env_matches_dotenv_values(name: str, tmp_path: Path) -> None:
    root = _project(tmp_path, BATTERY[name])
    assert read_env(root) == dict(dotenv_values(root / ".env", encoding="utf-8"))


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("name", sorted(BATTERY))
def test_load_env_matches_load_dotenv(
    name: str, override: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, BATTERY[name])
    monkeypatch.setenv("WP37_PRESET", "from-shell")
    expected: dict[str, Any] = {}
    for run in ("dotenv", "helper"):
        for var in NAMES[:3]:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("WP37_PRESET", "from-shell")
        if run == "dotenv":
            load_dotenv(root / ".env", override=override, encoding="utf-8")
        else:
            load_env(root, override=override)
        seen = {var: os.environ.get(var) for var in NAMES}
        if run == "dotenv":
            expected = seen
        else:
            assert seen == expected


def test_load_env_never_overrides_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, b"WP37_PRESET=from-file\n")
    monkeypatch.setenv("WP37_PRESET", "from-shell")
    load_env(root)
    assert os.environ["WP37_PRESET"] == "from-shell"
    load_env(root, override=True)
    assert os.environ["WP37_PRESET"] == "from-file"


def test_missing_file_is_a_no_op(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    before = dict(os.environ)
    load_env(root)
    assert read_env(root) == {}
    assert dict(os.environ) == before


def test_directory_named_env_is_treated_like_a_missing_file(tmp_path: Path) -> None:
    root = _project(tmp_path, None)
    (root / ".env").mkdir()
    assert read_env(root) == dict(dotenv_values(root / ".env", encoding="utf-8")) == {}
    load_env(root)


BAD_FILES: dict[str, bytes] = {
    "cp1252": b"caf\xe9=1\n",
    "latin1_value": b"WP37_A=caf\xe9\n",
    "utf16_with_bom": "WP37_A=1\n".encode("utf-16"),
    "utf16_without_bom": "WP37_A=1\n".encode("utf-16-le"),
    "nul_byte": b"WP37_A=1\x00\n",
    "lone_continuation": b"WP37_A=\x80\n",
}


@pytest.mark.parametrize("mode", ["load", "read"])
@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_unreadable_content_raises_one_friendly_error(name: str, mode: str, tmp_path: Path) -> None:
    root = _project(tmp_path, BAD_FILES[name])
    before = dict(os.environ)
    with pytest.raises(EnvFileError) as info:
        load_env(root) if mode == "load" else read_env(root)
    assert dict(os.environ) == before, "nothing from a bad file may be left in the environment"
    text = str(info.value)
    assert str(root / ".env") in text
    assert "UTF-8" in text
    assert info.value.path == root / ".env"
    for raw in ("codec", "0xe9", "0xff", "0x80", "embedded null", "position"):
        assert raw not in text, text
    assert "\n" not in text.strip()


def test_decode_error_is_wrapped_with_its_cause(tmp_path: Path) -> None:
    root = _project(tmp_path, BAD_FILES["cp1252"])
    with pytest.raises(EnvFileError) as info:
        load_env(root)
    assert isinstance(info.value.cause, UnicodeDecodeError)
    assert info.value.__cause__ is info.value.cause


def test_unreadable_file_reports_the_operating_system_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, b"WP37_A=1\n")
    real_open = open

    def deny(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file).endswith(".env"):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny)
    for call in (load_env, read_env):
        with pytest.raises(EnvFileError) as info:
            call(root)
        assert "Permission denied" in str(info.value)
        assert str(root / ".env") in str(info.value)
        assert isinstance(info.value.cause, PermissionError)


def test_partial_load_is_rolled_back(tmp_path: Path) -> None:
    root = _project(tmp_path, b"WP37_A=1\nWP37_B=2\x00\n")
    with pytest.raises(EnvFileError):
        load_env(root)
    assert "WP37_A" not in os.environ
    assert "WP37_B" not in os.environ


def test_rollback_restores_overridden_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, b"WP37_PRESET=changed\nWP37_B=2\x00\n")
    monkeypatch.setenv("WP37_PRESET", "from-shell")
    with pytest.raises(EnvFileError):
        load_env(root, override=True)
    assert os.environ["WP37_PRESET"] == "from-shell"


def test_hint_names_the_command_when_one_is_given() -> None:
    exc = UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")
    _reason, hint = describe_env_read_error(exc, "sync")
    assert hint is not None and "'dango sync'" in hint
    _reason, generic = describe_env_read_error(exc, None)
    assert generic is not None and "dango" not in generic and "UTF-8" in generic


def test_error_text_is_plain_and_rich_safe_after_escaping(tmp_path: Path) -> None:
    from rich.markup import escape
    from rich.text import Text

    root = tmp_path / "[bold]proj[/bold]"
    root.mkdir()
    (root / ".env").write_bytes(BAD_FILES["cp1252"])
    with pytest.raises(EnvFileError) as info:
        load_env(root)
    assert Text.from_markup(escape(str(info.value))).plain == str(info.value)


def test_parse_env_text_matches_dotenv_values_for_text() -> None:
    import io

    text = BATTERY["interpolation"].decode("utf-8")
    assert parse_env_text(text) == dict(dotenv_values(stream=io.StringIO(text)))
    assert parse_env_text("") == {}
