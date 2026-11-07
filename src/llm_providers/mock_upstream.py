"""Mock LLM upstream speaking both provider wire formats.

    python -m llm_providers.mock_upstream --port 9100

Endpoints:
  POST /openai/v1/chat/completions   (OpenAI-compatible; streaming and non-streaming)
  POST /anthropic/v1/messages        (Anthropic Messages; streaming and non-streaming)
  GET  /healthz

Streams are cut into random 1-9 byte network chunks, splitting lines and UTF-8
sequences, to exercise the gateway parsers. See ``mock_wire`` for behaviors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from typing import AsyncIterator

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import mock_wire as w


async def _read(request: Request) -> tuple[dict, w.Behavior, str]:
    body = json.loads(await request.body())
    model = str(body.get("model", "mock-echo"))
    return body, w.parse_behavior(model), model


async def _drip(payload: bytes, seed: int) -> AsyncIterator[bytes]:
    for part in w.chunk_bytes(payload, random.Random(seed)):
        yield part
        await asyncio.sleep(0.002)


def _error(status: int) -> Response:
    headers = {"retry-after": "1"} if status == 429 else {}
    return JSONResponse({"error": {"type": "mock_error", "message": f"mock status {status}"}},
                        status_code=status, headers=headers)


def _prompt_tokens(messages: list[dict]) -> int:
    return w.estimate_tokens("".join(str(m.get("content", "")) for m in messages))


async def openai_chat(request: Request) -> Response:
    body, behavior, model = await _read(request)
    if behavior.delay_ms:
        await asyncio.sleep(behavior.delay_ms / 1000)
    if behavior.status != 200:
        return _error(behavior.status)
    messages = body.get("messages", [])
    text = w.reply_text(behavior, messages)
    if body.get("stream"):
        payload = w.openai_stream(text, model, _prompt_tokens(messages), random.Random(7), behavior.midstream_error)
        return StreamingResponse(_drip(payload, 11), media_type="text/event-stream")
    return JSONResponse(w.openai_completion(text, model, _prompt_tokens(messages)))


async def anthropic_messages(request: Request) -> Response:
    body, behavior, model = await _read(request)
    if behavior.delay_ms:
        await asyncio.sleep(behavior.delay_ms / 1000)
    if behavior.status != 200:
        return _error(behavior.status)
    messages = body.get("messages", [])
    text = w.reply_text(behavior, messages)
    if body.get("stream"):
        payload = w.anthropic_stream(text, model, _prompt_tokens(messages), random.Random(7), behavior.midstream_error)
        return StreamingResponse(_drip(payload, 13), media_type="text/event-stream")
    return JSONResponse(w.anthropic_completion(text, model, _prompt_tokens(messages)))


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


app = Starlette(routes=[
    Route("/openai/v1/chat/completions", openai_chat, methods=["POST"]),
    Route("/anthropic/v1/messages", anthropic_messages, methods=["POST"]),
    Route("/healthz", healthz, methods=["GET"]),
])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Mock LLM upstream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9100)
    args = parser.parse_args(argv)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
