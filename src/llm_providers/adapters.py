"""Wire-format adapters for OpenAI-compatible Chat Completions and Anthropic Messages.

Stdlib only: each adapter builds the HTTP request (URL, headers, JSON body) and
interprets responses. The HTTP transport lives in ``http_provider.py``.

Formats implemented (verified against provider documentation):

OpenAI-compatible Chat Completions
  POST {base_url}/chat/completions, ``Authorization: Bearer <key>``.
  Streaming: ``data: {chat.completion.chunk}`` lines with text in
  ``choices[0].delta.content``; with ``stream_options.include_usage`` a final chunk
  with empty ``choices`` carries ``usage``; the stream ends with ``data: [DONE]``.

Anthropic Messages
  POST {base_url}/v1/messages, headers ``x-api-key``, ``anthropic-version: 2023-06-01``.
  Streaming events: message_start (usage.input_tokens), content_block_start,
  content_block_delta (delta.type == "text_delta"), content_block_stop,
  message_delta (usage.output_tokens), message_stop; ping and error may appear.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .sse import SSEEvent

ANTHROPIC_VERSION = "2023-06-01"


# --------------------------------------------------------------------------------------
# Neutral request/response types
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatRequest:
    messages: list[dict[str, str]]
    max_tokens: int
    model: str | None = None  # overrides the provider's configured model


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class UsageUpdate:
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class StreamDone:
    pass


StreamItem = TextDelta | UsageUpdate | StreamDone


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage | None
    model: str
    provider: str


# --------------------------------------------------------------------------------------
# Errors (messages are internal; gateways must not forward them to clients)
# --------------------------------------------------------------------------------------

class ProviderError(Exception):
    kind = "provider_error"


class ProviderHTTPError(ProviderError):
    kind = "http_status"

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"upstream returned HTTP {status}")


class ProviderTransportError(ProviderError):
    kind = "transport"


class ProviderProtocolError(ProviderError):
    kind = "protocol"


# --------------------------------------------------------------------------------------
# Provider configuration
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: Literal["openai", "anthropic"]
    base_url: str
    api_key: str
    model: str
    max_tokens_field: str = "max_completion_tokens"  # OpenAI-compatible only
    extra_headers: dict[str, str] = field(default_factory=dict)

    def model_for(self, req: ChatRequest) -> str:
        return req.model or self.model


class Adapter(Protocol):
    def build(self, cfg: ProviderConfig, req: ChatRequest, stream: bool) -> tuple[str, dict[str, str], dict[str, Any]]: ...
    def interpret(self, ev: SSEEvent) -> list[StreamItem]: ...
    def parse_completion(self, cfg: ProviderConfig, req: ChatRequest, body: Any) -> Completion: ...


def _loads(data: str) -> Any:
    try:
        return json.loads(data)
    except ValueError:
        raise ProviderProtocolError("malformed JSON in upstream event") from None


def _int_or_none(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


class OpenAIAdapter:
    def build(self, cfg: ProviderConfig, req: ChatRequest, stream: bool) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = cfg.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg.api_key}", "Content-Type": "application/json", **cfg.extra_headers}
        body: dict[str, Any] = {
            "model": cfg.model_for(req),
            "messages": [{"role": m["role"], "content": m["content"]} for m in req.messages],
            cfg.max_tokens_field: req.max_tokens,
        }
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return url, headers, body

    def interpret(self, ev: SSEEvent) -> list[StreamItem]:
        data = ev.data.strip()
        if data == "[DONE]":
            return [StreamDone()]
        obj = _loads(data)
        if not isinstance(obj, dict):
            raise ProviderProtocolError("unexpected chunk shape")
        if "error" in obj:
            raise ProviderProtocolError("upstream error event")
        items: list[StreamItem] = []
        for choice in obj.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str) and content:
                items.append(TextDelta(content))
        usage = obj.get("usage")
        if isinstance(usage, dict):
            items.append(UsageUpdate(_int_or_none(usage.get("prompt_tokens")), _int_or_none(usage.get("completion_tokens"))))
        return items

    def parse_completion(self, cfg: ProviderConfig, req: ChatRequest, body: Any) -> Completion:
        try:
            text = body["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderProtocolError("unexpected completion shape") from None
        usage = None
        u = body.get("usage") if isinstance(body, dict) else None
        if isinstance(u, dict):
            pi, co = _int_or_none(u.get("prompt_tokens")), _int_or_none(u.get("completion_tokens"))
            if pi is not None and co is not None:
                usage = Usage(pi, co)
        return Completion(text=str(text), usage=usage, model=str(body.get("model") or cfg.model_for(req)), provider=cfg.name)


class AnthropicAdapter:
    def build(self, cfg: ProviderConfig, req: ChatRequest, stream: bool) -> tuple[str, dict[str, str], dict[str, Any]]:
        url = cfg.base_url.rstrip("/") + "/v1/messages"
        headers = {
            "x-api-key": cfg.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
            **cfg.extra_headers,
        }
        system = "\n\n".join(m["content"] for m in req.messages if m["role"] == "system")
        body: dict[str, Any] = {
            "model": cfg.model_for(req),
            "max_tokens": req.max_tokens,
            "messages": [{"role": m["role"], "content": m["content"]} for m in req.messages if m["role"] != "system"],
        }
        if system:
            body["system"] = system
        if stream:
            body["stream"] = True
        return url, headers, body

    def interpret(self, ev: SSEEvent) -> list[StreamItem]:
        obj = _loads(ev.data)
        if not isinstance(obj, dict):
            raise ProviderProtocolError("unexpected event shape")
        etype = obj.get("type")
        if etype == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str) and delta["text"]:
                return [TextDelta(delta["text"])]
            return []
        if etype == "message_start":
            usage = ((obj.get("message") or {}).get("usage")) or {}
            return [UsageUpdate(input_tokens=_int_or_none(usage.get("input_tokens")))]
        if etype == "message_delta":
            usage = obj.get("usage") or {}
            return [UsageUpdate(output_tokens=_int_or_none(usage.get("output_tokens")))]
        if etype == "message_stop":
            return [StreamDone()]
        if etype == "error":
            raise ProviderProtocolError("upstream error event")
        return []  # ping, content_block_start/stop, unknown future events

    def parse_completion(self, cfg: ProviderConfig, req: ChatRequest, body: Any) -> Completion:
        if not isinstance(body, dict) or not isinstance(body.get("content"), list):
            raise ProviderProtocolError("unexpected message shape")
        text = "".join(b.get("text", "") for b in body["content"] if isinstance(b, dict) and b.get("type") == "text")
        usage = None
        u = body.get("usage")
        if isinstance(u, dict):
            it, ot = _int_or_none(u.get("input_tokens")), _int_or_none(u.get("output_tokens"))
            if it is not None and ot is not None:
                usage = Usage(it, ot)
        return Completion(text=text, usage=usage, model=str(body.get("model") or cfg.model_for(req)), provider=cfg.name)


ADAPTERS: dict[str, Adapter] = {"openai": OpenAIAdapter(), "anthropic": AnthropicAdapter()}
