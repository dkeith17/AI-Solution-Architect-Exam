"""MCP security gateway: authenticating, authorizing reverse proxy for Streamable HTTP.

Request flow for ``/mcp``:

    authenticate Bearer JWT -> (POST) read body with size cap -> policy.decide()
        Reject  -> JSON-RPC error returned directly; downstream never contacted
        Forward -> original bytes streamed to the downstream server; the downstream
                   response (JSON or SSE) is streamed back without buffering

GET and DELETE on ``/mcp`` (legacy SSE stream and session termination) are
authenticated and then forwarded. ``/healthz`` is unauthenticated.

The client's bearer token terminates at the gateway and is never forwarded.
Only an allowlist of headers crosses the proxy in either direction.
"""

from __future__ import annotations

import contextlib
import logging
import os
from dataclasses import dataclass
from typing import AsyncIterator

import httpx2
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .auth import AuthError, AuthSettings, authenticate
from .policy import Forward, Reject, decide, error_body

log = logging.getLogger("task2.gateway")

AUTH_FAILED_CODE = -32000      # implementation-defined server error range
UPSTREAM_FAILED_CODE = -32000

FORWARD_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "mcp-session-id",
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
    "last-event-id",
    "traceparent",
    "tracestate",
)
FORWARD_REQUEST_HEADER_PREFIXES = ("mcp-param-",)
FORWARD_RESPONSE_HEADERS = (
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "cache-control",
    "retry-after",
)


@dataclass(frozen=True)
class GatewaySettings:
    auth: AuthSettings
    downstream_url: str
    max_body_bytes: int = 4 * 1024 * 1024
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 300.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "GatewaySettings":
        env = os.environ if environ is None else environ
        secret = env.get("GATEWAY_JWT_SECRET")
        if not secret:
            raise RuntimeError("GATEWAY_JWT_SECRET is not set (generate one with: python -m task2_gateway.mint_token --gen-secret)")
        return cls(
            auth=AuthSettings(
                secret=secret,
                issuer=env.get("GATEWAY_JWT_ISSUER", "quilr-sa-exam"),
                audience=env.get("GATEWAY_JWT_AUDIENCE", "quilr-mcp-gateway"),
            ),
            downstream_url=env.get("GATEWAY_DOWNSTREAM_URL", "http://127.0.0.1:8001/mcp"),
            max_body_bytes=int(env.get("GATEWAY_MAX_BODY_BYTES", str(4 * 1024 * 1024))),
        )


class BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise BodyTooLarge
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _forward_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in request.headers.items():
        k = key.lower()
        if k in FORWARD_REQUEST_HEADERS or k.startswith(FORWARD_REQUEST_HEADER_PREFIXES):
            out[k] = value
    # Identity encoding lets the gateway relay bytes verbatim.
    out["accept-encoding"] = "identity"
    return out


def _unauthorized(reason: str) -> JSONResponse:
    return JSONResponse(
        error_body(None, AUTH_FAILED_CODE, "Unauthorized: missing, invalid, or expired bearer token"),
        status_code=401,
        headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{reason}"'},
    )


def create_app(settings: GatewaySettings) -> Starlette:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        timeout = httpx2.Timeout(settings.connect_timeout_s, read=settings.read_timeout_s)
        async with httpx2.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            app.state.http = client
            log.info("gateway ready; downstream=%s", settings.downstream_url)
            yield

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def mcp_endpoint(request: Request) -> Response:
        try:
            principal = authenticate(request.headers.get("authorization"), settings.auth)
        except AuthError as exc:
            log.warning("auth rejected: reason=%s client=%s", exc.reason, request.client.host if request.client else "?")
            return _unauthorized(exc.reason)

        body = b""
        request_id = None
        if request.method == "POST":
            try:
                body = await _read_body(request, settings.max_body_bytes)
            except BodyTooLarge:
                return JSONResponse(error_body(None, -32600, "Invalid Request: body too large"), status_code=413)
            decision = decide(body, principal.role, request.headers)
            if isinstance(decision, Reject):
                log.warning(
                    "denied: sub=%s role=%s reason=%s tool=%s",
                    principal.subject, principal.role, decision.reason, decision.tool_name,
                )
                return JSONResponse(decision.body, status_code=decision.http_status)
            assert isinstance(decision, Forward)
            request_id = decision.request_id
            log.info(
                "forward: sub=%s role=%s method=%s tool=%s",
                principal.subject, principal.role, decision.method, decision.tool_name,
            )
        else:
            log.info("forward: sub=%s role=%s http=%s", principal.subject, principal.role, request.method)

        client: httpx2.AsyncClient = request.app.state.http
        upstream_request = client.build_request(
            request.method,
            settings.downstream_url,
            headers=_forward_headers(request),
            content=body if body else None,
        )
        try:
            upstream = await client.send(upstream_request, stream=True)
        except httpx2.HTTPError as exc:
            log.error("downstream unreachable: %s", type(exc).__name__)
            return JSONResponse(
                error_body(request_id, UPSTREAM_FAILED_CODE, "Downstream MCP server unavailable"),
                status_code=502,
            )

        response_headers = {k: upstream.headers[k] for k in FORWARD_RESPONSE_HEADERS if k in upstream.headers}
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream.aclose),
        )

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/mcp", mcp_endpoint, methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )
