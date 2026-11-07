"""End-to-end: mock upstream (both wire formats) -> Task 3 gateway -> client."""

from __future__ import annotations

import json
import time

import httpx2
import pytest

from llm_providers.mock_wire import PII_TEXT
from llm_providers.sse import SSEParser
from task3_llm_stream.redactor import redact

pytestmark = pytest.mark.integration


@pytest.fixture
def stack(servers) -> str:
    upstream = servers.start(["-m", "llm_providers.mock_upstream"])
    gw = servers.start(["-m", "task3_llm_stream"], env={
        "MOCK_UPSTREAM_URL": f"http://127.0.0.1:{upstream}",
        "T3_PROVIDERS": "mock-openai,mock-anthropic",
    })
    return f"http://127.0.0.1:{gw}/v1/chat/stream"


def stream(url: str, body: dict) -> tuple[int, list[tuple[str, dict, float]], float]:
    parser = SSEParser()
    events = []
    start = time.perf_counter()
    with httpx2.stream("POST", url, json=body, timeout=30) as r:
        if r.status_code != 200:
            return r.status_code, [("http", json.loads(r.read()), 0.0)], 0.0
        for chunk in r.iter_bytes():
            for ev in parser.feed(chunk):
                events.append((ev.event, json.loads(ev.data), time.perf_counter() - start))
    return 200, events, time.perf_counter() - start


def body(model: str, provider: str) -> dict:
    return {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 128, "model": model, "provider": provider}


@pytest.mark.parametrize("provider", ["mock-openai", "mock-anthropic"])
def test_pii_is_redacted_end_to_end(stack: str, provider: str) -> None:
    status, events, total = stream(stack, body("mock-pii", provider))
    assert status == 200
    text = "".join(d["text"] for e, d, _ in events if e == "delta")
    assert text == redact(PII_TEXT)[0]
    for secret in ("jane.doe@example.com", "123-45-6789", "4111 1111", "5555-5555"):
        assert secret not in text
    done = events[-1]
    assert done[0] == "done" and done[1]["redactions"] == {"email": 2, "ssn": 1, "card": 2}
    assert done[1]["usage"]["input_tokens"] > 0


@pytest.mark.parametrize("provider", ["mock-openai", "mock-anthropic"])
def test_stream_is_incremental(stack: str, provider: str) -> None:
    status, events, total = stream(stack, body("mock-echo", provider) | {
        "messages": [{"role": "user", "content": "word " * 200}]})
    deltas = [(d, t) for e, d, t in events if e == "delta"]
    assert len(deltas) > 10
    first_delta_time = deltas[0][1]
    assert first_delta_time < total * 0.5  # first token long before the stream ends


def test_upstream_429_before_first_byte_becomes_http_429(stack: str) -> None:
    status, events, _ = stream(stack, body("mock-429", "mock-openai"))
    assert status == 429
    assert events[0][1]["error"]["type"] == "upstream_rate_limited"


@pytest.mark.parametrize("provider", ["mock-openai", "mock-anthropic"])
def test_midstream_upstream_error_becomes_error_event(stack: str, provider: str) -> None:
    status, events, _ = stream(stack, body("mock-pii-midstream-error", provider))
    assert status == 200
    assert events[-1][0] == "error"
    assert "mock upstream failure" not in json.dumps(events) and "Overloaded" not in json.dumps(events)


@pytest.mark.parametrize("bad", [
    {"messages": []},
    {"messages": [{"role": "hacker", "content": "x"}]},
    {"messages": [{"role": "user", "content": "x"}], "max_tokens": 0},
    {"messages": [{"role": "user", "content": "x"}], "provider": "openai"},   # not enabled
])
def test_invalid_requests(stack: str, bad: dict) -> None:
    r = httpx2.post(stack, json=bad)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request"
