"""tests/unit/test_error_text.py

dango.utils.error_text: redacted-then-escaped error text for logs and Rich output.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from dango.utils.error_text import redacted_error_text, rich_error_text

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("GET https://h.example.com/v1/x?api_key=SECRET123&page=2 failed", "SECRET123"),
        ("GET https://h.example.com/x?access_token=TOK123456&a=1", "TOK123456"),
        ("GET https://h.example.com/x?client_secret=CSEC123456&a=1", "CSEC123456"),
        ("GET https://h.example.com/x?signature=SIG1234567", "SIG1234567"),
        ("could not connect to postgresql://user:PASSw0rd9@host:5432/db", "PASSw0rd9"),
        ("request failed, Authorization: Bearer abcDEF123456xyz", "abcDEF123456xyz"),
        ("password=hunter22hunter", "hunter22hunter"),
    ],
)
def test_pattern_secrets_are_masked(text: str, secret: str) -> None:
    assert secret not in redacted_error_text(None, text)
    assert secret not in rich_error_text(None, RuntimeError(text))


def test_known_secret_value_from_project_files_is_masked(tmp_path: Path) -> None:
    (tmp_path / ".dlt").mkdir()
    (tmp_path / ".dlt" / "secrets.toml").write_text(
        '[a]\nk = "KnownValue12345"\n', encoding="utf-8"
    )
    (tmp_path / ".env").write_text("OTHER=EnvValue67890\n", encoding="utf-8")
    text = "failed with KnownValue12345 and EnvValue67890"
    out = redacted_error_text(tmp_path, text)
    assert "KnownValue12345" not in out and "EnvValue67890" not in out


def test_ordinary_text_keeps_host_path_and_status() -> None:
    text = "503 Server Error for url: https://api.example.com/v1/items?page=2 (host unavailable)"
    assert redacted_error_text(None, text) == text
    assert rich_error_text(None, text) == text


def test_markup_is_escaped_and_renders_literally() -> None:
    text = "boom [/x] [red]oops[/red] [foo]"
    escaped = rich_error_text(None, text)
    console = Console(width=200, record=True, file=StringIO())
    console.print(f"[red]{escaped}[/red]")
    assert text in console.export_text()


def test_output_stays_ascii_for_ascii_input() -> None:
    out = rich_error_text(None, "x?api_key=SECRET123456&b=1 postgresql://u:p4ssword@h/db")
    assert out.isascii()
