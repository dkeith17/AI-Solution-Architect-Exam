"""Provider stream items -> redacted client SSE frames. No web-framework dependency."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

from llm_providers.adapters import ProviderError, StreamDone, StreamItem, TextDelta, UsageUpdate

from .redactor import StreamRedactor

log = logging.getLogger("task3.gateway")


def sse_frame(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode("utf-8")


async def redacted_events(first: StreamItem | None, upstream: AsyncIterator[StreamItem],
                          provider: str, max_holdback: int) -> AsyncIterator[bytes]:
    """Turn provider stream items into redacted client SSE frames."""
    redactor = StreamRedactor(max_holdback=max_holdback)
    usage: dict[str, int] = {}
    item = first
    try:
        while item is not None and not isinstance(item, StreamDone):
            if isinstance(item, TextDelta):
                out = redactor.feed(item.text)
                if out:
                    yield sse_frame("delta", {"text": out})
            elif isinstance(item, UsageUpdate):
                if item.input_tokens is not None:
                    usage["input_tokens"] = item.input_tokens
                if item.output_tokens is not None:
                    usage["output_tokens"] = item.output_tokens
            try:
                item = await upstream.__anext__()
            except StopAsyncIteration:
                item = None
        tail = redactor.flush()
        if tail:
            yield sse_frame("delta", {"text": tail})
        log.info("stream complete provider=%s redactions=%s", provider, dict(redactor.counts))
        yield sse_frame("done", {"provider": provider, "redactions": dict(redactor.counts), "usage": usage})
    except ProviderError as exc:
        # Emit what is already buffered (redacted), then a sanitized error event.
        tail = redactor.flush()
        if tail:
            yield sse_frame("delta", {"text": tail})
        log.warning("stream aborted provider=%s error=%s", provider, exc)
        yield sse_frame("error", {"error": {"type": "upstream_error", "message": "The model provider stream failed."}})
    finally:
        aclose = getattr(upstream, "aclose", None)
        if aclose is not None:
            await aclose()
