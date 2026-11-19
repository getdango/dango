"""dango/utils/tls_errors.py

Recognise certificate-verification failures (typically a TLS-inspecting corporate proxy) and say what to do.

Level 0 module. Guidance never suggests turning certificate verification off.
"""

from __future__ import annotations

import ssl
import subprocess
from typing import Any

from rich.markup import escape

_MARKERS = (
    "certificate_verify_failed",
    "certificate verify failed",
    "self-signed certificate",
    "self signed certificate",
    "unable to get local issuer certificate",
    "unable to get issuer certificate",
)
_MAX_DEPTH = 8


def is_certificate_error_text(text: str) -> bool:
    """True when *text* (a message or tool output) names a certificate-verification failure."""
    lowered = text.lower()
    return any(marker in lowered for marker in _MARKERS)


def _children(exc: BaseException) -> list[BaseException]:
    found: list[BaseException] = []
    for candidate in (exc.__cause__, exc.__context__, getattr(exc, "reason", None), *exc.args):
        if isinstance(candidate, BaseException):
            found.append(candidate)
    return found


def is_certificate_error(exc: BaseException) -> bool:
    """True when *exc*, or an exception it wraps, is a certificate-verification failure."""
    seen: set[int] = set()
    stack: list[tuple[BaseException, int]] = [(exc, 0)]
    while stack:
        current, depth = stack.pop()
        if id(current) in seen or depth > _MAX_DEPTH:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if is_certificate_error_text(str(current)):
            return True
        stack.extend((child, depth + 1) for child in _children(current))
    return False


def certificate_guidance_lines() -> list[str]:
    """Plain ASCII lines explaining a certificate failure and the supported fixes."""
    return [
        "A certificate check failed. This usually means a corporate firewall or proxy is",
        "inspecting HTTPS traffic with its own certificate that Python does not trust.",
        "Ask your IT team for the organisation's root certificate (a .pem or .crt file), then:",
        "  - set SSL_CERT_FILE to that file (used by Python's ssl module and pip), and",
        "    REQUESTS_CA_BUNDLE to the same file (used by requests); or",
        "  - for pip only: pip install --cert <file> <package>",
        "Then run the command again. Certificate checks stay on.",
    ]


def print_certificate_guidance(console: Any = None) -> None:
    """Print the guidance (Rich-escaped) to *console*, or to a new stdout console."""
    if console is None:
        from rich.console import Console

        console = Console()
    for line in certificate_guidance_lines():
        console.print(f"[yellow]{escape(line)}[/yellow]", soft_wrap=True)


def print_guidance_for_install_failure(error: subprocess.CalledProcessError) -> bool:
    """Print the guidance when a failed pip run's captured output is a certificate failure."""
    raw = error.stderr if error.stderr is not None else b""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
    if not is_certificate_error_text(text):
        return False
    print_certificate_guidance()
    return True
