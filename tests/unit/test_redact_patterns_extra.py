"""tests/unit/test_redact_patterns_extra.py

Redaction of signed-URL, key-block and token shapes, plus linear-time behaviour on huge input.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from dango.utils.error_text import rich_error_text
from dango.utils.redact import redact_patterns

pytestmark = pytest.mark.unit

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n"
PAT = "patAbCdEfGhIjKlMn." + "0123456789abcdef" * 4


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        (
            "GET https://h.example.com/b?sv=2022-11-02&ss=b&sig=SIGNATUREVALUE12%3D",
            "SIGNATUREVALUE12",
        ),
        (
            "GET https://h/b/k?X-Amz-Signature=abcdef0123456789abcdef&X-Amz-Date=1",
            "abcdef0123456789abcdef",
        ),
        (
            "GET https://h/b/k?X-Amz-Credential=AKIAEXAMPLE1234%2F20260101%2Fus%2Fs3&a=1",
            "AKIAEXAMPLE1234",
        ),
        ("GET https://h/b/k?X-Goog-Signature=deadbeef0123456789&a=1", "deadbeef0123456789"),
        ("GET https://h.example.com/x?auth=AUTHVALUE1234&a=1", "AUTHVALUE1234"),
        ("failed url /v1/x%3Fapi_key%3DENCODEDSECRET1%26page%3D2", "ENCODEDSECRET1"),
        (f"bad key:\n{PEM}", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"),
        (
            '{"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADAN\\n-----END PRIVATE KEY-----\\n"}',
            "MIIEvQIBADAN",
        ),
        ("bad key -----BEGIN RSA PRIVATE KEY-----\\nMIIEowIBAAKCAQEA", "MIIEowIBAAKCAQEA"),
        ("private_key=abcdef0123456", "abcdef0123456"),
        (f"Airtable error for token {PAT} rejected", PAT),
    ],
)
def test_pattern_masked(text: str, secret: str) -> None:
    assert secret not in redact_patterns(text)


def test_normal_text_is_unchanged() -> None:
    for text in (
        "401 Client Error for url: https://api.example.com/v1/auth/login?page=2&author=bob",
        "table signature_log has no column sigma; design pattern mismatch",
        "patrol officers path /v1/pattern/items status 500",
        "Could not parse -----BEGIN CERTIFICATE----- header",
    ):
        assert redact_patterns(text) == text


_PROBE = """
import sys, time
from dango.utils.redact import redact_patterns
chunks = ["a_", "a-", "_-", "password_", "token-", "Ab9_-", "=", " ", "api_key", "?key=", "%3D"]
worst = 0.0
for chunk in chunks:
    text = chunk * (1_000_000 // len(chunk)) + " password=hunter22hunter"
    start = time.perf_counter()
    out = redact_patterns(text)
    worst = max(worst, time.perf_counter() - start)
    assert "hunter22hunter" not in out, chunk
blob = ("Zm9vYmFy-_" * 30_000) + " token=abcdef123456"
start = time.perf_counter()
assert "abcdef123456" not in redact_patterns(blob)
worst = max(worst, time.perf_counter() - start)
print(worst)
"""


def test_one_megabyte_adversarial_input_is_linear() -> None:
    """Runs in a child process so a quadratic regex fails the test instead of hanging it."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=60, check=False
        )
    except subprocess.TimeoutExpired:
        pytest.fail("redact_patterns did not finish adversarial 1 MB inputs within 60 s")
    assert proc.returncode == 0, proc.stderr
    assert float(proc.stdout.strip()) < 2.0


def test_printed_error_text_is_capped() -> None:
    out = rich_error_text(None, "x" * 100_000 + " password=hunter22hunter")
    assert len(out) < 10_000
    assert "hunter22hunter" not in out
    assert "truncated" in out
