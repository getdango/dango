"""dango/web/middleware/__init__.py

ASGI middleware for the Dango web server.
"""

from dango.web.middleware.rate_limit import RateLimitMiddleware

__all__ = ["RateLimitMiddleware"]
