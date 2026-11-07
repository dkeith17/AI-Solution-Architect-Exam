"""Provider wire-format adapters, round-tripped through the mock's exact bytes."""

from __future__ import annotations

import random

import pytest

from llm_providers import mock_wire as w
from llm_providers.adapters import (
    ADAPTERS,
    ANTHROPIC_VERSION,
    ChatRequest,
    ProviderConfig,
    ProviderProtocolError,
    StreamDone,
    TextDelta,
    UsageUpdate,
)
from llm_providers.config import provider_config
from llm_providers.sse import SSEEvent, SSEParser

REQ = ChatRequest(messages=[{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}], max_tokens=50)
OPENAI = ProviderConfig(name="o", kind="openai", base_url="https://api.example/v1/", api_key="k1", model="m1")
ANTHROPIC = ProviderConfig(name="a", kind="anthropic", base_url="https://api.example", api_key="k2", model="m2")
TEXT = "Hello \u00e9\u20ac\U0001f600 world, this is a streamed reply."


def run(kind: str, payload: bytes, seed: int) -> tuple[str, dict, bool]:
    adapter = ADAPTERS[kind]
    parser = SSEParser()
    text, usage, done = [], {}, False
    for chunk in w.chunk_bytes(payload, random.Random(seed)):
        for ev in parser.feed(chunk):
            for item in adapter.interpret(ev):
                if isinstance(item, TextDelta):
                    text.append(item.text)
                elif isinstance(item, UsageUpdate):
                    usage.update({k: v for k, v in (("in", item.input_tokens), ("out", item.output_tokens)) if v is not None})
                elif isinstance(item, StreamDone):
                    done = True
    return "".join(text), usage, done


def test_openai_build_streaming_request() -> None:
    url, headers, body = ADAPTERS["openai"].build(OPENAI, REQ, stream=True)
    assert url == "https://api.example/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k1"
    assert body["model"] == "m1" and body["max_completion_tokens"] == 50
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["messages"][0] == {"role": "system", "content": "be brief"}


def test_openai_max_tokens_field_configurable() -> None:
    cfg = ProviderConfig(name="o", kind="openai", base_url="b", api_key="k", model="m", max_tokens_field="max_tokens")
    _, _, body = ADAPTERS["openai"].build(cfg, REQ, stream=False)
    assert body["max_tokens"] == 50 and "max_completion_tokens" not in body and "stream" not in body


def test_anthropic_build_moves_system_prompt() -> None:
    url, headers, body = ADAPTERS["anthropic"].build(ANTHROPIC, REQ, stream=True)
    assert url == "https://api.example/v1/messages"
    assert headers["x-api-key"] == "k2" and headers["anthropic-version"] == ANTHROPIC_VERSION == "2023-06-01"
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 50 and body["stream"] is True


def test_request_model_override() -> None:
    _, _, body = ADAPTERS["openai"].build(OPENAI, ChatRequest(REQ.messages, 5, model="other"), stream=False)
    assert body["model"] == "other"


@pytest.mark.parametrize("seed", range(25))
def test_openai_stream_round_trip(seed: int) -> None:
    payload = w.openai_stream(TEXT, "m", 12, random.Random(seed))
    text, usage, done = run("openai", payload, seed)
    assert text == TEXT and done and usage == {"in": 12, "out": w.estimate_tokens(TEXT)}


@pytest.mark.parametrize("seed", range(25))
def test_anthropic_stream_round_trip(seed: int) -> None:
    payload = w.anthropic_stream(TEXT, "m", 12, random.Random(seed))
    text, usage, done = run("anthropic", payload, seed)
    assert text == TEXT and done and usage == {"in": 12, "out": w.estimate_tokens(TEXT)}


@pytest.mark.parametrize("kind, builder", [("openai", w.openai_stream), ("anthropic", w.anthropic_stream)])
def test_midstream_error_event_raises(kind, builder) -> None:
    payload = builder(TEXT, "m", 1, random.Random(0), midstream_error=True)
    with pytest.raises(ProviderProtocolError):
        run(kind, payload, 0)


def test_openai_ignores_empty_and_role_only_deltas() -> None:
    a = ADAPTERS["openai"]
    assert a.interpret(SSEEvent("message", '{"choices":[{"delta":{"role":"assistant","content":""}}]}')) == []
    assert a.interpret(SSEEvent("message", "[DONE]")) == [StreamDone()]


def test_anthropic_ignores_ping_and_non_text_deltas() -> None:
    a = ADAPTERS["anthropic"]
    assert a.interpret(SSEEvent("ping", '{"type":"ping"}')) == []
    assert a.interpret(SSEEvent("content_block_delta",
                                '{"type":"content_block_delta","delta":{"type":"input_json_delta","partial_json":"{"}}')) == []


@pytest.mark.parametrize("kind", ["openai", "anthropic"])
def test_malformed_event_json_raises(kind) -> None:
    with pytest.raises(ProviderProtocolError):
        ADAPTERS[kind].interpret(SSEEvent("message", "{not json"))


def test_parse_completions() -> None:
    c = ADAPTERS["openai"].parse_completion(OPENAI, REQ, w.openai_completion("hi there", "m", 9))
    assert (c.text, c.usage.input_tokens, c.usage.output_tokens, c.provider) == ("hi there", 9, 2, "o")
    c = ADAPTERS["anthropic"].parse_completion(ANTHROPIC, REQ, w.anthropic_completion("hi there", "m", 9))
    assert (c.text, c.usage.total) == ("hi there", 11)
    with pytest.raises(ProviderProtocolError):
        ADAPTERS["openai"].parse_completion(OPENAI, REQ, {"choices": []})
    with pytest.raises(ProviderProtocolError):
        ADAPTERS["anthropic"].parse_completion(ANTHROPIC, REQ, {"nope": 1})


def test_provider_config_requires_keys_and_models_for_real_providers() -> None:
    with pytest.raises(RuntimeError):
        provider_config("openai", {})
    with pytest.raises(RuntimeError):
        provider_config("anthropic", {"ANTHROPIC_API_KEY": "k"})
    cfg = provider_config("anthropic", {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_MODEL": "m"})
    assert cfg.base_url == "https://api.anthropic.com"
    mock = provider_config("mock-openai", {"MOCK_OPENAI_MODEL": "mock-429"})
    assert mock.model == "mock-429" and mock.base_url == "http://127.0.0.1:9100/openai/v1"


@pytest.mark.parametrize("model, status, delay, text_mode", [
    ("mock-echo", 200, 0, "echo"),
    ("mock-pii-slow-250", 200, 250, "pii"),
    ("mock-429", 429, 0, "echo"),
    ("mock-500", 500, 0, "echo"),
    # Regression: delay values that contain "500" or "429" are not status directives.
    ("mock-slow-1500", 200, 1500, "echo"),
    ("mock-slow-5000", 200, 5000, "echo"),
    ("mock-slow-4290", 200, 4290, "echo"),
    ("mock-slow-1500-429", 429, 1500, "echo"),
    ("mock-500-slow-200", 500, 200, "echo"),
])
def test_mock_behavior_parsing(model: str, status: int, delay: int, text_mode: str) -> None:
    b = w.parse_behavior(model)
    assert (b.status, b.delay_ms, b.text_mode) == (status, delay, text_mode)


def test_mock_midstream_error_flag() -> None:
    assert w.parse_behavior("mock-pii-midstream-error").midstream_error
    assert not w.parse_behavior("mock-pii").midstream_error
