"""dango/ingestion/status_codes.py

Read an HTTP status code out of an error message without mistaking a PID, a port or a
temp-directory name for one.

Level 0 module (no Dango imports). Local code: runs on Windows, macOS and Linux; the cloud
scheduler also reaches it through the sync error classifier.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=64)
def _pattern(codes: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(code) for code in codes)
    return re.compile(rf"(?<![0-9A-Za-z])(?:{alternatives})(?![0-9A-Za-z])")


def has_status_code(text: str, *codes: str) -> bool:
    """True if any of *codes* appears in *text* as a whole number.

    A code matches when the characters on both sides are not ASCII digits or letters, so
    ``HTTP 429``, ``status: 429``, ``429 Too Many Requests`` and ``error code=429,`` match while
    ``pid 40187``, ``/tmp/pytest-4291/x``, ``1429`` and ``a429b`` do not. Words such as
    ``unauthorized`` or ``rate limit`` are not handled here; callers keep matching those as
    substrings.
    """
    return _pattern(codes).search(text) is not None
