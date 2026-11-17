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


@pytest.mark.parametrize(
    "text",
    [
        '{"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQ',
        "private_key: -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\n",
        "private_key = -----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxyz\n-----END RSA PRIVATE KEY-----",
        "{'type': 'service_account', 'private_key': '-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQ\\n-----END PRIVATE KEY-----\\n'}",
        "secret: -----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIBkg4LVWM9nuwNSk3yByxZpYRTBnVJk5\n-----END EC PRIVATE KEY-----",
    ],
)
def test_key_body_is_masked_when_a_key_name_precedes_the_block(text: str) -> None:
    out = redact_patterns(text)
    for body in ("MIIEvQIBADAN", "MIIEowIBAAKC", "MHcCAQEEIBkg"):
        assert body not in out


def test_long_name_suffix_is_still_matched() -> None:
    assert "PRODSECRET1" not in redact_patterns(
        "api_key_for_production_environment_us_east=PRODSECRET1"
    )
    suffix = "x" * 40
    assert "LONGSUFFIX1" not in redact_patterns(f"password_{suffix}=LONGSUFFIX1")


@pytest.mark.parametrize(
    "text",
    [
        "x_api_key=KEYVALUE12",
        "myapi_key=KEYVALUE12",
        "refresh_token=KEYVALUE12",
        "CLIENT_SECRET: KEYVALUE12",
        "password_hash=KEYVALUE12",
        "secret_access_key_id=KEYVALUE12",
    ],
)
def test_existing_name_shapes_unchanged(text: str) -> None:
    assert "KEYVALUE12" not in redact_patterns(text)


def test_sas_parameter_and_encoded_ampersand_stop() -> None:
    assert "SASVALUE123" not in redact_patterns("GET https://h/b?sas=SASVALUE123&a=1")
    out = redact_patterns("url /v1/x%3Fapi_key%3DENCSECRET12%26page%3D2")
    assert "ENCSECRET12" not in out and "%26page%3D2" in out


def test_cap_applies_after_redaction_and_notice_is_literal() -> None:
    from io import StringIO

    from rich.console import Console

    text = "x" * 7990 + "KNOWNSECRETVALUE" + "y" * 500
    out = rich_error_text(None, text.replace("KNOWNSECRETVALUE", "password=hunter22hunter"))
    assert "hunter22" not in out and "password=hunt" not in out.replace("password=****", "")
    buf = StringIO()
    Console(file=buf, width=20000).print(f"[red]{out}[/red]")
    assert "[truncated" in buf.getvalue()


def test_known_secret_straddling_the_cap_is_not_partly_shown(tmp_path) -> None:
    (tmp_path / ".env").write_text("TOKEN=sk_live_ABCDEFGHIJKLMNOP\n", encoding="utf-8")
    text = "x" * 7990 + "sk_live_ABCDEFGHIJKLMNOP" + "y" * 100
    out = rich_error_text(tmp_path, text)
    assert "sk_live" not in out


@pytest.mark.parametrize(
    ("tail", "secret"),
    [
        ("api_key=TAILSECRET123", "TAILSECRET123"),
        ("API-KEY: TAILSECRET123", "TAILSECRET123"),
        ("private_key=TAILSECRET123", "TAILSECRET123"),
        ("secret: TAILSECRET123", "TAILSECRET123"),
        ("token=TAILSECRET123", "TAILSECRET123"),
    ],
)
def test_cut_off_key_block_does_not_hide_a_following_secret_line(tail: str, secret: str) -> None:
    text = f"-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQ\n{tail}"
    out = redact_patterns(text)
    assert secret not in out
    assert "MIIEvQIBADAN" not in out


def test_name_suffix_bound_covers_sixty_characters() -> None:
    assert "SIXTYSECRET1" not in redact_patterns(f"api_key_{'z' * 55}=SIXTYSECRET1")
