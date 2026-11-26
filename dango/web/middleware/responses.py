"""dango/web/middleware/responses.py

ASGI responses AuthMiddleware sends itself: 401, 403 and 503 JSON bodies and the login redirect.
"""

from __future__ import annotations

import json
from typing import Any

from dango.exceptions import AuthDatabaseUnavailableError


def make_401_json() -> Any:
    """Create a 401 Unauthorized JSON ASGI response."""
    from starlette.responses import Response

    body = json.dumps(
        {
            "error_code": "DANGO-S001",
            "message": "Authentication required.",
        }
    )
    return Response(content=body, status_code=401, media_type="application/json")


def make_503_json() -> Any:
    """Create a 503 JSON ASGI response for a missing auth database (no paths)."""
    from starlette.responses import Response

    body = json.dumps(
        {
            "error_code": AuthDatabaseUnavailableError._default_error_code,
            "message": "Authentication database unavailable",
        }
    )
    return Response(content=body, status_code=503, media_type="application/json")


def make_redirect_response(location: str) -> Any:
    """Create a 302 redirect ASGI response."""
    from starlette.responses import RedirectResponse

    return RedirectResponse(url=location, status_code=302)


def make_403_json(message: str) -> Any:
    """Create a 403 Forbidden JSON ASGI response."""
    from starlette.responses import Response

    body = json.dumps(
        {
            "error_code": "DANGO-S002",
            "message": message,
        }
    )
    return Response(content=body, status_code=403, media_type="application/json")
