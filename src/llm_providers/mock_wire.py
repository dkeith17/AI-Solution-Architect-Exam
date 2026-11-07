"""Pure helpers for the mock upstream: behavior parsing and provider-format payloads.

The mock's behavior is selected by the request's model name, so tests and demos can
choose it per request:

  mock-echo            reply "Echo: <last user message>"
  mock-pii             reply with text containing an email, SSN and card numbers
  mock-429             respond HTTP 429 with Retry-After: 1
  mock-500             respond HTTP 500
  mock-slow-<ms>       wait <ms> milliseconds before responding (combinable)
  mock-midstream-error stream a few deltas, then a provider error event
"""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass

PII_TEXT = (
    "Sure. The account owner is jane.doe@example.com and her backup address is "
    "j.doe+billing@mail.example.co.uk. SSN on file: 123-45-6789. "
    "Card: 4111 1111 1111 1111, alternate card 5555-5555-5555-4444. "
    "Order number 1234-5678 and phone extension 4242 are not sensitive."
)


@dataclass(frozen=True)
class Behavior:
    status: int = 200
    delay_ms: int = 0
    text_mode: str = "echo"  # echo | pii
    midstream_error: bool = False


def parse_behavior(model: str) -> Behavior:
    """Parse behavior directives from dash-separated segments of the model name.

    Directives match whole segments, so a delay value such as the ``1500`` in
    ``mock-slow-1500`` is never mistaken for the ``500`` status directive.
    """
    m = model.lower()
    delay = 0
    found = re.search(r"(?:^|-)slow-(\d{1,6})(?=-|$)", m)
    if found:
        delay = int(found.group(1))
        m = m[:found.start()] + m[found.end():]   # drop the delay value before reading other segments
    segments = set(m.split("-"))
    status = 429 if "429" in segments else 500 if "500" in segments else 200
    return Behavior(
        status=status,
        delay_ms=delay,
        text_mode="pii" if "pii" in segments else "echo",
        midstream_error="midstream-error" in model.lower(),
    )


def reply_text(behavior: Behavior, messages: list[dict]) -> str:
    if behavior.text_mode == "pii":
        return PII_TEXT
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    return f"Echo: {last_user}"


def estimate_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def split_text(text: str, rng: random.Random, lo: int = 1, hi: int = 6) -> list[str]:
    pieces, i = [], 0
    while i < len(text):
        n = rng.randint(lo, hi)
        pieces.append(text[i:i + n])
        i += n
    return pieces


def chunk_bytes(payload: bytes, rng: random.Random, lo: int = 1, hi: int = 9) -> list[bytes]:
    """Split a byte stream at arbitrary points (mid-line, mid-UTF-8) to stress parsers."""
    out, i = [], 0
    while i < len(payload):
        n = rng.randint(lo, hi)
        out.append(payload[i:i + n])
        i += n
    return out


def _sse(data: dict | str, event: str | None = None) -> str:
    body = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n"


def openai_stream(text: str, model: str, prompt_tokens: int, rng: random.Random,
                  midstream_error: bool = False) -> bytes:
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 0, "model": model}
    parts = [": mock keep-alive comment\n\n",
             _sse({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]})]
    pieces = split_text(text, rng)
    for n, piece in enumerate(pieces):
        if midstream_error and n == 3:
            parts.append(_sse({"error": {"message": "mock upstream failure", "type": "server_error"}}))
            return "".join(parts).encode("utf-8")
        parts.append(_sse({**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}))
    parts.append(_sse({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))
    completion = estimate_tokens(text)
    parts.append(_sse({**base, "choices": [], "usage": {
        "prompt_tokens": prompt_tokens, "completion_tokens": completion, "total_tokens": prompt_tokens + completion}}))
    parts.append(_sse("[DONE]"))
    return "".join(parts).encode("utf-8")


def anthropic_stream(text: str, model: str, prompt_tokens: int, rng: random.Random,
                     midstream_error: bool = False) -> bytes:
    parts = [
        _sse({"type": "message_start", "message": {
            "id": "msg_mock", "type": "message", "role": "assistant", "content": [], "model": model,
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": prompt_tokens, "output_tokens": 1}}}, "message_start"),
        _sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}, "content_block_start"),
        _sse({"type": "ping"}, "ping"),
    ]
    for n, piece in enumerate(split_text(text, rng)):
        if midstream_error and n == 3:
            parts.append(_sse({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}, "error"))
            return "".join(parts).encode("utf-8")
        parts.append(_sse({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": piece}}, "content_block_delta"))
    parts.append(_sse({"type": "content_block_stop", "index": 0}, "content_block_stop"))
    parts.append(_sse({"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                       "usage": {"output_tokens": estimate_tokens(text)}}, "message_delta"))
    parts.append(_sse({"type": "message_stop"}, "message_stop"))
    return "".join(parts).encode("utf-8")


def openai_completion(text: str, model: str, prompt_tokens: int) -> dict:
    completion = estimate_tokens(text)
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion, "total_tokens": prompt_tokens + completion},
    }


def anthropic_completion(text: str, model: str, prompt_tokens: int) -> dict:
    return {
        "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": prompt_tokens, "output_tokens": estimate_tokens(text)},
    }
