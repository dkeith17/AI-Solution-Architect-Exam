"""HTTP transport for the adapters, built on httpx2 (the HTTP client mcp v2 ships with).

Raises only ``ProviderError`` subclasses. Their messages are for server logs;
gateways convert them to standardized client-facing errors.
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx2

from .adapters import (
    ADAPTERS,
    ChatRequest,
    Completion,
    ProviderConfig,
    ProviderHTTPError,
    ProviderProtocolError,
    ProviderTransportError,
    StreamDone,
    StreamItem,
)
from .sse import SSEParser


def _retry_after(headers: httpx2.Headers) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # HTTP-date form is not interpreted
    return seconds if seconds >= 0 else None


class HTTPProvider:
    def __init__(self, cfg: ProviderConfig, client: httpx2.AsyncClient) -> None:
        self.cfg = cfg
        self.client = client
        self.adapter = ADAPTERS[cfg.kind]

    @property
    def name(self) -> str:
        return self.cfg.name

    async def stream(self, req: ChatRequest) -> AsyncIterator[StreamItem]:
        url, headers, body = self.adapter.build(self.cfg, req, stream=True)
        try:
            async with self.client.stream("POST", url, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    raise ProviderHTTPError(resp.status_code, _retry_after(resp.headers))
                parser = SSEParser()
                async for chunk in resp.aiter_bytes():
                    try:
                        events = parser.feed(chunk)
                    except ValueError:
                        raise ProviderProtocolError("invalid SSE stream") from None
                    for ev in events:
                        for item in self.adapter.interpret(ev):
                            yield item
                            if isinstance(item, StreamDone):
                                return
                for ev in parser.close():
                    for item in self.adapter.interpret(ev):
                        yield item
        except httpx2.HTTPError as exc:
            raise ProviderTransportError(type(exc).__name__) from None

    async def complete(self, req: ChatRequest) -> Completion:
        url, headers, body = self.adapter.build(self.cfg, req, stream=False)
        try:
            resp = await self.client.post(url, headers=headers, json=body)
        except httpx2.HTTPError as exc:
            raise ProviderTransportError(type(exc).__name__) from None
        if resp.status_code != 200:
            raise ProviderHTTPError(resp.status_code, _retry_after(resp.headers))
        try:
            payload = resp.json()
        except ValueError:
            raise ProviderProtocolError("upstream body is not JSON") from None
        return self.adapter.parse_completion(self.cfg, req, payload)
