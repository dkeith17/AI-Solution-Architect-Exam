"""LLM gateway streaming endpoint with real-time PII redaction.

POST /v1/chat/stream
    {"messages": [{"role": "user", "content": "..."}],
     "max_tokens": 256, "provider": "mock-openai", "model": "optional override"}

Response: ``text/event-stream`` with events
    event: delta  data: {"text": "..."}                 (already redacted)
    event: done   data: {"provider": ..., "redactions": {...}, "usage": {...}}
    event: error  data: {"error": {"type": ..., "message": ...}}

Latency design: the upstream stream is consumed chunk by chunk; each chunk goes
through ``StreamRedactor.feed`` and whatever is safe is written immediately. The
full response is never accumulated. Before the HTTP response starts, the first
upstream item is awaited ("priming") so that failures before the first byte (429,
connection refused) become proper HTTP status codes instead of a 200 stream that
immediately errors.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from llm_providers.adapters import ChatRequest, ProviderError, ProviderHTTPError, StreamItem
from llm_providers.config import PROVIDER_NAMES, provider_config
from llm_providers.http_provider import HTTPProvider

from .streaming import redacted_events

log = logging.getLogger("task3.gateway")

ROLES = ("system", "user", "assistant")


@dataclass(frozen=True)
class Settings:
    providers: tuple[str, ...]
    default_provider: str
    max_tokens_limit: int = 4096
    max_holdback: int = 256

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environ is None else environ
        providers = tuple(p.strip() for p in env.get("T3_PROVIDERS", "mock-openai,mock-anthropic").split(",") if p.strip())
        for p in providers:
            if p not in PROVIDER_NAMES:
                raise RuntimeError(f"unknown provider in T3_PROVIDERS: {p}")
        default = env.get("T3_DEFAULT_PROVIDER", providers[0])
        if default not in providers:
            raise RuntimeError("T3_DEFAULT_PROVIDER must be listed in T3_PROVIDERS")
        return cls(providers=providers, default_provider=default,
                   max_tokens_limit=int(env.get("T3_MAX_TOKENS_LIMIT", "4096")))


def _error(status: int, etype: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"type": etype, "message": message}}, status_code=status, headers=headers)


def parse_request(payload: Any, settings: Settings) -> tuple[str, ChatRequest] | str:
    """Validate the client body. Returns (provider, request) or an error message."""
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty array"
    clean: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict) or m.get("role") not in ROLES or not isinstance(m.get("content"), str):
            return "each message needs role (system|user|assistant) and string content"
        clean.append({"role": m["role"], "content": m["content"]})
    max_tokens = payload.get("max_tokens", 256)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= settings.max_tokens_limit:
        return f"max_tokens must be an integer from 1 to {settings.max_tokens_limit}"
    provider = payload.get("provider", settings.default_provider)
    if provider not in settings.providers:
        return f"provider must be one of {list(settings.providers)}"
    model = payload.get("model")
    if model is not None and (not isinstance(model, str) or not model or len(model) > 200):
        return "model must be a non-empty string"
    return provider, ChatRequest(messages=clean, max_tokens=max_tokens, model=model)


def _upstream_error_response(exc: ProviderError) -> Response:
    if isinstance(exc, ProviderHTTPError) and exc.status == 429:
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after is not None else None
        return _error(429, "upstream_rate_limited", "The model provider is rate limiting requests. Retry later.", headers)
    return _error(502, "upstream_error", "The model provider could not complete the request.")


def create_app(settings: Settings, environ: dict[str, str] | None = None) -> Starlette:
    configs = {name: provider_config(name, environ) for name in settings.providers}

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0, read=120.0)) as client:
            app.state.providers = {name: HTTPProvider(cfg, client) for name, cfg in configs.items()}
            yield

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok", "providers": list(settings.providers)})

    async def chat_stream(request: Request) -> Response:
        try:
            payload = json.loads(await request.body())
        except ValueError:
            return _error(400, "invalid_request", "body must be valid JSON")
        parsed = parse_request(payload, settings)
        if isinstance(parsed, str):
            return _error(400, "invalid_request", parsed)
        provider_name, chat = parsed
        provider: HTTPProvider = request.app.state.providers[provider_name]

        upstream = provider.stream(chat)
        try:
            first: StreamItem | None = await upstream.__anext__()
        except StopAsyncIteration:
            first = None
        except ProviderError as exc:
            log.warning("upstream failed before first byte provider=%s error=%s", provider_name, exc)
            await upstream.aclose()
            return _upstream_error_response(exc)

        return StreamingResponse(
            redacted_events(first, upstream, provider_name, settings.max_holdback),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            Route("/v1/chat/stream", chat_stream, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
