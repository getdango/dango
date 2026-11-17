"""dango/utils/stdio.py

Make the standard streams tolerant of text their encoding cannot represent.
"""

import sys


def ensure_safe_stdio() -> None:
    """Make stdout and stderr degrade instead of raising on non-encodable text.

    A Windows console or pipe often uses cp1252, which cannot encode the emoji
    in CLI output. ``errors="replace"`` substitutes a placeholder. The encoding
    itself is left alone (a real Windows console has its own wide-character
    path). Streams without ``reconfigure`` (test streams, some wrappers) are
    left untouched.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (OSError, ValueError):
            # Closed or otherwise unusable stream: nothing to make safe.
            continue
