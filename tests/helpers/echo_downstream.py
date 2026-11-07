"""Test-only downstream that records every request the gateway forwards.

    python tests/helpers/echo_downstream.py --port N

POST /mcp     -> records body + headers, answers a JSON-RPC result echoing the id
GET  /mcp     -> a short SSE stream (to test streaming relay)
GET  /_requests -> the recorded requests (bypasses the gateway; tests call it directly)
"""

from __future__ import annotations

import argparse
import asyncio
import json

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

RECORDED: list[dict] = []


async def mcp_post(request: Request):
    raw = await request.body()
    RECORDED.append({"body": raw.decode("utf-8"), "headers": dict(request.headers)})
    try:
        rid = json.loads(raw).get("id")
    except ValueError:
        rid = None
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "result": {"echo": True}},
        headers={"mcp-session-id": "sess-123", "x-internal-secret": "must-not-leak"},
    )


async def mcp_get(request: Request):
    RECORDED.append({"body": "", "headers": dict(request.headers), "http_method": "GET"})

    async def gen():
        for i in range(3):
            yield f"event: message\ndata: {{\"n\": {i}}}\n\n".encode()
            await asyncio.sleep(0.05)

    return StreamingResponse(gen(), media_type="text/event-stream")


async def requests(request: Request):
    return JSONResponse(RECORDED)


app = Starlette(routes=[
    Route("/mcp", mcp_post, methods=["POST"]),
    Route("/mcp", mcp_get, methods=["GET"]),
    Route("/_requests", requests, methods=["GET"]),
])

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, required=True)
    uvicorn.run(app, host="127.0.0.1", port=p.parse_args().port, log_level="warning")
