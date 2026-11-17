"""tests/unit/test_stdio.py

dango.utils.stdio.ensure_safe_stdio makes non-encodable text degrade instead of crash.
"""

import io
import sys

import pytest
from dango.utils.stdio import ensure_safe_stdio

pytestmark = pytest.mark.unit


def _cp1252_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252", write_through=True), raw


def test_cp1252_stdout_prints_emoji_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    out, raw = _cp1252_stream()
    err, _ = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)

    ensure_safe_stdio()
    print("done ✅ \U0001f361")
    print("warn ✅", file=sys.stderr)

    assert raw.getvalue().startswith(b"done ")
    assert out.encoding == "cp1252"


def test_stream_without_reconfigure_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = io.StringIO()
    assert not hasattr(plain, "reconfigure")
    monkeypatch.setattr(sys, "stdout", plain)
    monkeypatch.setattr(sys, "stderr", plain)

    ensure_safe_stdio()

    assert sys.stdout is plain
    print("✅")
    assert plain.getvalue() == "✅\n"


def test_utf8_stream_keeps_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="utf-8", write_through=True)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", out)

    ensure_safe_stdio()
    print("✅")

    assert raw.getvalue() == "✅\n".encode()
