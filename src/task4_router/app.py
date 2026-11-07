"""LLM gateway completion endpoint: tenant auth, token rate limiting, model failover.

POST /v1/completions
    Authorization: Bearer <tenant API key>
    {"messages": [{"role": "user", "content": "..."}], "max_tokens": 256}

Flow: authenticate tenant -> validate body -> reserve estimated tokens (SQLite,
atomic) -> route (primary, failover on 429/timeout) -> reconcile reservation to
provider-reported usage -> respond. Every error uses ``errors.error_response``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx2
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from llm_providers.adapters import ChatRequest
from llm_providers.config import PROVIDER_NAMES, provider_config
from llm_providers.http_provider import HTTPProvider

from .errors import error_response
from .ratelimit import Denied, TokenRateLimiter
from .router import AllProvidersFailed, ModelRouter
from .tokens import reservation_tokens

log = logging.getLogger("task4.gateway")

ROLES = ("system", "user", "assistant")


def _env_bool(env: dict[str, str], key: str) -> bool:
    return env.get(key, "false").strip().lower() in ("1", "true", "yes")


@dataclass(frozen=True)
class Settings:
    db_path: str
    primary: str
    secondary: str
    primary_timeout_ms: int = 3000
    secondary_timeout_ms: int = 3000
    failover_on_5xx: bool = False
    failover_on_transport_error: bool = False
    max_tokens_limit: int = 4096
    window_seconds: float = 60.0
    sweep_interval_s: float = 30.0

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        s = cls(
            db_path=env.get("T4_DB_PATH", "task4_router.sqlite3"),
            primary=env.get("T4_PRIMARY_PROVIDER", "mock-openai"),
            secondary=env.get("T4_SECONDARY_PROVIDER", "mock-anthropic"),
            primary_timeout_ms=int(env.get("T4_PRIMARY_TIMEOUT_MS", "3000")),
            secondary_timeout_ms=int(env.get("T4_SECONDARY_TIMEOUT_MS", "3000")),
            failover_on_5xx=_env_bool(env, "T4_FAILOVER_ON_5XX"),
            failover_on_transport_error=_env_bool(env, "T4_FAILOVER_ON_TRANSPORT_ERROR"),
            max_tokens_limit=int(env.get("T4_MAX_TOKENS_LIMIT", "4096")),
            window_seconds=float(env.get("T4_WINDOW_SECONDS", "60")),
        )
        for p in (s.primary, s.secondary):
            if p not in PROVIDER_NAMES:
                raise RuntimeError(f"unknown provider {p!r}")
        if s.primary == s.secondary:
            raise RuntimeError("primary and secondary providers must differ")
        return s


def parse_body(payload: Any, max_tokens_limit: int) -> ChatRequest | str:
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    unknown = set(payload) - {"messages", "max_tokens"}
    if unknown:
        return f"unknown fields: {sorted(unknown)}"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array"
    clean: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES or not isinstance(m.get("content"), str):
            return "each message needs role (system|user|assistant) and string content"
        clean.append({"role": m["role"], "content": m["content"]})
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= max_tokens_limit:
        return f"max_tokens must be an integer from 1 to {max_tokens_limit}"
    return ChatRequest(messages=clean, max_tokens=max_tokens)


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def create_app(settings: Settings, environ: dict[str, str] | None = None) -> Starlette:
    limiter = TokenRateLimiter(settings.db_path, window_seconds=settings.window_seconds)
    primary_cfg = provider_config(settings.primary, environ)
    secondary_cfg = provider_config(settings.secondary, environ)

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(settings.sweep_interval_s)
            try:
                removed = await asyncio.to_thread(limiter.sweep)
                if removed:
                    log.debug("swept %d expired usage rows", removed)
            except Exception:  # keep sweeping; never crash the app
                log.exception("sweep failed")

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # Per-call deadlines are enforced by the router; these are outer safety nets.
        timeout = httpx2.Timeout(30.0)
        async with httpx2.AsyncClient(timeout=timeout) as client:
            app.state.router = ModelRouter(
                primary=HTTPProvider(primary_cfg, client),
                secondary=HTTPProvider(secondary_cfg, client),
                primary_timeout_s=settings.primary_timeout_ms / 1000,
                secondary_timeout_s=settings.secondary_timeout_ms / 1000,
                failover_on_5xx=settings.failover_on_5xx,
                failover_on_transport_error=settings.failover_on_transport_error,
            )
            task = asyncio.create_task(sweeper())
            try:
                yield
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "primary": settings.primary, "secondary": settings.secondary})

    async def completions(request: Request) -> Response:
        rid = uuid.uuid4().hex
        api_key = _bearer(request.headers.get("authorization"))
        tenant = await asyncio.to_thread(limiter.lookup_tenant, api_key) if api_key else None
        if tenant is None:
            return error_response("authentication_error", rid)

        try:
            payload = json.loads(await request.body())
        except ValueError:
            return error_response("invalid_request", rid, detail="body must be valid JSON")
        parsed = parse_body(payload, settings.max_tokens_limit)
        if isinstance(parsed, str):
            return error_response("invalid_request", rid, detail=parsed)

        needed = reservation_tokens(parsed.messages, parsed.max_tokens)
        reservation = await asyncio.to_thread(limiter.reserve, tenant, needed)
        if isinstance(reservation, Denied):
            log.info("rate limited tenant=%s reason=%s used=%d need=%d limit=%d",
                     tenant.tenant_id, reservation.reason, reservation.used, needed, reservation.limit)
            if reservation.reason == "exceeds_limit":
                return error_response("request_too_large", rid)
            return error_response("rate_limit_exceeded", rid, retry_after=reservation.retry_after)

        router: ModelRouter = request.app.state.router
        try:
            result = await router.complete(parsed)
        except AllProvidersFailed as exc:
            if exc.upstream_did_no_work:
                await asyncio.to_thread(limiter.release, reservation)
            last = exc.attempts[-1]
            log.warning("request %s failed tenant=%s attempts=%s", rid, tenant.tenant_id, exc)
            if last.outcome == "timeout":
                return error_response("upstream_timeout", rid)
            if last.outcome == "rate_limited":
                return error_response("upstream_rate_limited", rid, retry_after=last.retry_after)
            return error_response("upstream_error", rid)

        usage = result.completion.usage
        actual = usage.total if usage is not None else needed
        await asyncio.to_thread(limiter.reconcile, reservation, actual)
        remaining = max(0, tenant.tokens_per_minute - await asyncio.to_thread(limiter.used, tenant.tenant_id))
        log.info("request %s ok tenant=%s provider=%s failover=%s tokens=%d",
                 rid, tenant.tenant_id, result.completion.provider, result.failed_over, actual)
        body = {
            "id": rid,
            "provider": result.completion.provider,
            "model": result.completion.model,
            "content": result.completion.text,
            "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens} if usage else None,
            "failover": result.failed_over,
        }
        return JSONResponse(body, headers={
            "X-Request-ID": rid,
            "X-Gateway-Provider": result.completion.provider,
            "X-RateLimit-Limit-Tokens": str(tenant.tokens_per_minute),
            "X-RateLimit-Remaining-Tokens": str(remaining),
        })

    async def http_exception(request: Request, exc: Exception) -> Response:
        status = getattr(exc, "status_code", 500)
        etype = {404: "not_found", 405: "method_not_allowed"}.get(status, "invalid_request")
        return error_response(etype, uuid.uuid4().hex)

    async def unhandled(request: Request, exc: Exception) -> Response:
        rid = uuid.uuid4().hex
        log.exception("unhandled error request_id=%s", rid)
        return error_response("internal_error", rid)

    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/v1/completions", completions, methods=["POST"]),
        ],
        lifespan=lifespan,
        exception_handlers={HTTPException: http_exception, Exception: unhandled},
    )
    app.state.limiter = limiter
    return app
