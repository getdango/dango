"""tests/unit/test_status_code_match.py

Table test of ``has_status_code``: an HTTP status code matches only as a whole number, never
inside a longer digit run (a PID, a port, a temp-directory name) or glued to identifier letters.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("HTTP 429", "429"),
        ("http 429 too many requests", "429"),
        ("status: 429", "429"),
        ("429 Too Many Requests", "429"),
        ("error code=429, retry later", "429"),
        ("429", "429"),  # the whole string
        ("failed with 429", "429"),  # end of string
        ("429: slow down", "429"),
        ("(429)", "429"),
        ("[401] unauthorized", "401"),
        ("Status 403.", "403"),
        ("HTTP/1.1 404 Not Found", "404"),
        ("HTTPError 429\nsecond line", "429"),
    ],
)
def test_whole_number_matches(text: str, code: str) -> None:
    from dango.ingestion.status_codes import has_status_code

    assert has_status_code(text, code)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("pid 40187", "401"),
        ("(PID 140387) holds the lock", "403"),
        ("/tmp/pytest-4291/x", "429"),
        ("1429", "429"),
        ("4290", "429"),
        ("a429b", "429"),
        ("x429", "429"),
        ("429x", "429"),
        ("req4040001", "404"),
        ("nothing here", "429"),
        ("", "429"),
    ],
)
def test_embedded_digits_do_not_match(text: str, code: str) -> None:
    from dango.ingestion.status_codes import has_status_code

    assert not has_status_code(text, code)


def test_any_of_several_codes_matches() -> None:
    from dango.ingestion.status_codes import has_status_code

    assert has_status_code("got 403 from the api", "401", "403")
    assert not has_status_code("pid 40187 and port 14039", "401", "403")


def test_uppercase_text_matches_the_same_way() -> None:
    from dango.ingestion.status_codes import has_status_code

    assert has_status_code("HTTP 429 TOO MANY REQUESTS", "429")
    assert not has_status_code("PID40187", "401")
