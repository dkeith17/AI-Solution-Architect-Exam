"""Provider items -> redacted SSE frames, including mid-stream failure handling."""

from __future__ import annotations

import asyncio
import json

import pytest

from llm_providers.adapters import ProviderProtocolError, StreamDone, TextDelta, UsageUpdate
from llm_providers.mock_wire import PII_TEXT
from llm_providers.sse import SSEParser
from task3_llm_stream.redactor import redact
from task3_llm_stream.streaming import redacted_events

pytestmark = pytest.mark.anyio


class FakeUpstream:
    def __init__(self, items, fail_after: int | None = None) -> None:
        self.items = list(items)
        self.fail_after = fail_after
        self.n = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.fail_after is not None and self.n >= self.fail_after:
            raise ProviderProtocolError("boom")
        if self.n >= len(self.items):
            raise StopAsyncIteration
        item = self.items[self.n]
        self.n += 1
        await asyncio.sleep(0)
        return item

    async def aclose(self) -> None:
        self.closed = True


async def collect(first, upstream) -> list[tuple[str, dict]]:
    parser = SSEParser()
    events = []
    async for frame in redacted_events(first, upstream, "fake", 256):
        for ev in parser.feed(frame):
            events.append((ev.event, json.loads(ev.data)))
    return events


def pieces(text: str, size: int = 3) -> list[TextDelta]:
    return [TextDelta(text[i:i + size]) for i in range(0, len(text), size)]


async def test_full_stream_redacted_and_done_reports_counts() -> None:
    items = pieces(PII_TEXT) + [UsageUpdate(10, None), UsageUpdate(None, 40), StreamDone()]
    up = FakeUpstream(items[1:])
    events = await collect(items[0], up)
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert text == redact(PII_TEXT)[0]
    kind, done = events[-1]
    assert kind == "done"
    assert done["redactions"] == {"email": 2, "ssn": 1, "card": 2}
    assert done["usage"] == {"input_tokens": 10, "output_tokens": 40}
    assert up.closed


async def test_deltas_are_emitted_incrementally() -> None:
    items = pieces("word " * 50)
    events = await collect(items[0], FakeUpstream(items[1:]))
    deltas = [d for e, d in events if e == "delta"]
    assert len(deltas) > 20  # not accumulated into one frame


async def test_midstream_failure_flushes_redacted_buffer_then_error() -> None:
    items = pieces("contact jane@example.com about it", 4)
    up = FakeUpstream(items[1:], fail_after=5)
    events = await collect(items[0], up)
    text = "".join(d["text"] for e, d in events if e == "delta")
    assert "jane@" not in text
    assert events[-1][0] == "error"
    assert events[-1][1] == {"error": {"type": "upstream_error", "message": "The model provider stream failed."}}
    assert "boom" not in json.dumps(events)  # internal error text never leaks
    assert up.closed


async def test_empty_stream() -> None:
    events = await collect(None, FakeUpstream([]))
    assert [e for e, _ in events] == ["done"]
