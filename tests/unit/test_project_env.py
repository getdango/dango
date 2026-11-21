"""tests/unit/test_project_env.py

The one safe project `.env` loader: same results as python-dotenv for valid UTF-8 files, and one
friendly `EnvFileError` (names the file, says plain UTF-8) for every file that cannot be read.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values, load_dotenv

from dango.utils.project_env import (
    EnvFileError,
    describe_env_read_error,
    env_has,
    load_env,
    parse_env_text,
    read_env,
    read_env_text,
    set_env_value,
    unset_env_value,
)

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

    root = tmp_path / "[bold]proj"
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


def test_env_has_checks_the_environment_then_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, b"WP37_A=1\nWP37_B=\n")
    assert env_has(root, "WP37_A") is True
    assert env_has(root, "WP37_B") is False, "an empty value is not set"
    assert env_has(root, "WP37_C") is False
    monkeypatch.setenv("WP37_C", "x")
    assert env_has(root, "WP37_C") is True


def test_env_has_does_not_read_the_file_when_the_environment_has_the_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _project(tmp_path, BAD_FILES["cp1252"])
    monkeypatch.setenv("WP37_A", "x")
    assert env_has(root, "WP37_A") is True
    with pytest.raises(EnvFileError):
        env_has(root, "WP37_B")


# --- read mode never touches the process environment (review L1/L3) -------------------------


@pytest.mark.parametrize("name", sorted(BATTERY))
def test_read_env_leaves_the_environment_untouched(name: str, tmp_path: Path) -> None:
    root = _project(tmp_path, BATTERY[name])
    before = dict(os.environ)
    read_env(root)
    assert dict(os.environ) == before


def test_read_env_failure_does_not_roll_back_other_environment_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only load_env restores the environment; a failed read must not undo anyone's writes."""
    root = _project(tmp_path, b"WP37_A=1\n")

    def failing_values(*_args: Any, **_kwargs: Any) -> Any:
        os.environ["WP37_B"] = "written-meanwhile"
        raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

    monkeypatch.setattr("dotenv.dotenv_values", failing_values)
    with pytest.raises(EnvFileError):
        read_env(root)
    assert os.environ["WP37_B"] == "written-meanwhile"
    monkeypatch.delenv("WP37_B", raising=False)


# --- read_env_text / set_env_value / unset_env_value (review OS1) ---------------------------

TEXT_BATTERY: dict[str, bytes | None] = {
    "missing": None,
    "empty": b"",
    "plain": b"WP37_A=1\nWP37_B=2\n",
    "no_trailing_newline": b"WP37_A=1\nWP37_B=2",
    "crlf": b"WP37_A=1\r\nWP37_B=2\r\n",
    "export_prefix": b"export WP37_A=1\nWP37_B=2\n",
    "quotes": b"WP37_A='one two'\nWP37_B=\"three\"\n",
    "comments": b"# header\n\nWP37_A=1  # trailing\n# tail\n",
    "duplicates": b"WP37_A=1\nWP37_A=2\nWP37_B=3\n",
    "bom": b"\xef\xbb\xbfWP37_A=1\n",
    "non_ascii": "WP37_A=café\n".encode(),
    "multiline": b'WP37_A="x\ny"\nWP37_B=2\n',
    "invalid_line": b"not a pair\nWP37_A=1\n",
}
VALUES = ["plain", "with space", "it's", 'say "hi"', "back\\slash", "café —", "a#b", ""]


def _file(tmp_path: Path, tag: str, data: bytes | None) -> Path:
    path = tmp_path / tag / ".env"
    path.parent.mkdir(parents=True)
    if data is not None:
        path.write_bytes(data)
    return path


@pytest.mark.parametrize("name", sorted(TEXT_BATTERY))
def test_read_env_text_matches_read_text(name: str, tmp_path: Path) -> None:
    path = _file(tmp_path, "a", TEXT_BATTERY[name])
    expected = path.read_text(encoding="utf-8") if path.exists() else ""
    assert read_env_text(path) == expected


@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_read_env_text_rejects_unreadable_content(name: str, tmp_path: Path) -> None:
    path = _file(tmp_path, "a", BAD_FILES[name])
    with pytest.raises(EnvFileError) as info:
        read_env_text(path)
    assert str(path) in str(info.value) and "UTF-8" in str(info.value)


def test_read_env_text_reports_a_directory_and_a_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / ".env"
    folder.mkdir()
    with pytest.raises(EnvFileError):
        read_env_text(folder)
    path = _file(tmp_path, "b", b"A=1\n")
    real_open = open

    def deny(file: Any, *args: Any, **kwargs: Any) -> Any:
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", deny)
    with pytest.raises(EnvFileError, match="Permission denied"):
        read_env_text(path)


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("key", ["WP37_A", "WP37_NEW"])
@pytest.mark.parametrize("name", sorted(TEXT_BATTERY))
def test_set_env_value_matches_dotenv_set_key(
    name: str, key: str, value: str, tmp_path: Path
) -> None:
    from dotenv import set_key

    mine = _file(tmp_path, "mine", TEXT_BATTERY[name])
    theirs = _file(tmp_path, "theirs", TEXT_BATTERY[name])
    set_env_value(mine, key, value)
    set_key(str(theirs), key, value)
    assert mine.read_bytes() == theirs.read_bytes()


@pytest.mark.parametrize("key", ["WP37_A", "WP37_MISSING"])
@pytest.mark.parametrize("name", sorted(TEXT_BATTERY))
def test_unset_env_value_matches_dotenv_unset_key(name: str, key: str, tmp_path: Path) -> None:
    from dotenv import unset_key

    mine = _file(tmp_path, "mine", TEXT_BATTERY[name])
    theirs = _file(tmp_path, "theirs", TEXT_BATTERY[name])
    removed = unset_env_value(mine, key)
    expected_removed, _ = unset_key(str(theirs), key) if theirs.exists() else (None, key)
    assert removed is bool(expected_removed)
    assert mine.exists() == theirs.exists()
    if mine.exists():
        assert mine.read_bytes() == theirs.read_bytes()


def _no_leftovers(path: Path) -> None:
    assert [p.name for p in path.parent.iterdir() if p.name != path.name] == []


@pytest.mark.parametrize("action", ["set", "unset"])
@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_unreadable_file_is_never_rewritten(name: str, action: str, tmp_path: Path) -> None:
    path = _file(tmp_path, "a", BAD_FILES[name])
    path.chmod(0o600)
    before = path.read_bytes()
    with pytest.raises(EnvFileError) as info:
        if action == "set":
            set_env_value(path, "WP37_A", "new")
        else:
            unset_env_value(path, "WP37_A")
    assert "Cannot read" in str(info.value) and "UTF-8" in str(info.value)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    _no_leftovers(path)


def test_failed_replace_leaves_the_file_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _file(tmp_path, "a", b"WP37_A=1\nWP37_B=2\n")
    before = path.read_bytes()

    def deny(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("os.replace", deny)
    for call in (
        lambda: set_env_value(path, "WP37_A", "changed"),
        lambda: unset_env_value(path, "WP37_A"),
    ):
        with pytest.raises(EnvFileError) as info:
            call()
        assert "Cannot write" in str(info.value) and "Permission denied" in str(info.value)
        assert path.read_bytes() == before
        _no_leftovers(path)


def test_failure_while_writing_leaves_the_file_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dotenv.main

    path = _file(tmp_path, "a", b"WP37_A=1\nWP37_B=2\nWP37_C=3\n")
    before = path.read_bytes()
    real = dotenv.main.with_warn_for_invalid_lines

    def dies_after_first(mappings: Any) -> Any:
        for count, mapping in enumerate(real(mappings)):
            if count == 1:
                raise OSError(28, "No space left on device")
            yield mapping

    monkeypatch.setattr(dotenv.main, "with_warn_for_invalid_lines", dies_after_first)
    with pytest.raises(EnvFileError, match="No space left"):
        set_env_value(path, "WP37_B", "changed")
    assert path.read_bytes() == before
    _no_leftovers(path)


def test_unset_of_a_missing_file_or_key_is_false_and_writes_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "nothing" / ".env"
    assert unset_env_value(missing, "WP37_A") is False
    assert not missing.exists()
    path = _file(tmp_path, "a", b"WP37_A=1\n")
    assert unset_env_value(path, "WP37_B") is False
    assert path.read_bytes() == b"WP37_A=1\n"


def test_set_env_value_creates_a_missing_file(tmp_path: Path) -> None:
    path = _file(tmp_path, "a", None)
    set_env_value(path, "WP37_A", "x y")
    assert path.read_bytes() == b"WP37_A='x y'\n"
