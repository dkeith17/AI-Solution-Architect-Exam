"""Standardized gateway error payloads.

Every error the gateway returns has the same shape and never includes upstream
bodies, exception text, stack traces, provider URLs, or internal identifiers:

    {"error": {"type": "<stable code>", "message": "<fixed human text>", "request_id": "<uuid>"}}
"""

from __future__ import annotations

import math

from starlette.responses import JSONResponse

MESSAGES = {
    "authentication_error": "A valid API key is required.",
    "invalid_request": "The request body is invalid.",
    "request_too_large": "The request needs more tokens than the tenant's per-minute limit allows.",
    "rate_limit_exceeded": "Token rate limit exceeded. Retry after the indicated delay.",
    "upstream_rate_limited": "All model providers are rate limiting requests. Retry later.",
    "upstream_timeout": "The model providers did not respond in time.",
    "upstream_error": "The model provider could not complete the request.",
    "not_found": "Not found.",
    "method_not_allowed": "Method not allowed.",
    "internal_error": "The gateway encountered an internal error.",
}

STATUS = {
    "authentication_error": 401,
    "invalid_request": 400,
    "request_too_large": 400,
    "rate_limit_exceeded": 429,
    "upstream_rate_limited": 503,
    "upstream_timeout": 504,
    "upstream_error": 502,
    "not_found": 404,
    "method_not_allowed": 405,
    "internal_error": 500,
}


def error_response(etype: str, request_id: str, *, retry_after: float | None = None,
                   detail: str | None = None) -> JSONResponse:
    """``detail`` is only for gateway-authored validation text, never upstream text."""
    body = {"error": {"type": etype, "message": detail or MESSAGES[etype], "request_id": request_id}}
    headers = {"X-Request-ID": request_id}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return JSONResponse(body, status_code=STATUS[etype], headers=headers)
